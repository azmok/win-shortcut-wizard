import os
import re
import json
import sqlite3
import asyncio
import winreg
import subprocess
import threading
import flet as ft

DB_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "shortcut_wizard.db")

# インデクサーの排他制御用
is_indexing = False
indexer_lock = threading.Lock()

def init_db():
    """データベースのテーブルを初期化する"""
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS search_cache (
                query TEXT PRIMARY KEY,
                results TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS shortcuts (
                alias TEXT PRIMARY KEY,
                exe_path TEXT,
                registered_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                is_deleted INTEGER DEFAULT 0
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS custom_paths (
                path TEXT PRIMARY KEY
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS apps_index (
                path TEXT PRIMARY KEY,
                filename TEXT,
                last_scanned TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.commit()

def add_custom_path_to_db(path: str):
    """カスタムパスをデータベースに追加"""
    try:
        with sqlite3.connect(DB_FILE) as conn:
            cursor = conn.cursor()
            cursor.execute("INSERT OR REPLACE INTO custom_paths (path) VALUES (?)", (path,))
            conn.commit()
    except Exception as e:
        print(f"DB Write Error (custom_paths): {e}")

def delete_custom_path_from_db(path: str):
    """カスタムパスをデータベースから削除"""
    try:
        with sqlite3.connect(DB_FILE) as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM custom_paths WHERE path = ?", (path,))
            conn.commit()
    except Exception as e:
        print(f"DB Delete Error (custom_paths): {e}")

def get_custom_paths_from_db() -> list[str]:
    """データベースからすべてのカスタムパスを取得"""
    try:
        with sqlite3.connect(DB_FILE) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT path FROM custom_paths")
            return [row[0] for row in cursor.fetchall()]
    except Exception as e:
        print(f"DB Read Error (custom_paths): {e}")
        return []

def build_apps_index_in_background(custom_dirs: list[str], on_complete_callback=None):
    """バックグラウンドで指定フォルダ以下の.exeファイルをスキャンし、インデックスDBを構築する"""
    global is_indexing
    
    with indexer_lock:
        if is_indexing:
            print("Indexer is already running. Skipping this build request.")
            if on_complete_callback:
                on_complete_callback(False)
            return
        is_indexing = True

    default_dirs = [
        "$env:ProgramFiles",
        "${env:ProgramFiles(x86)}",
        "$env:LOCALAPPDATA\\Programs",
        "$env:LOCALAPPDATA"
    ]
    
    all_dirs_ps = []
    for d in default_dirs:
        all_dirs_ps.append(f'"{d}"')
    for d in custom_dirs:
        escaped_d = d.replace("'", "''")
        all_dirs_ps.append(f'"{escaped_d}"')
        
    dirs_array_string = ", ".join(all_dirs_ps)

    ps_script = f"""
    $sh = New-Object -ComObject WScript.Shell
    $results = @()

    $shortcutPaths = @(
        "$env:APPDATA\\Microsoft\\Windows\\Start Menu\\Programs",
        "$env:ProgramData\\Microsoft\\Windows\\Start Menu\\Programs",
        "$env:USERPROFILE\\Desktop",
        "C:\\Users\\Public\\Desktop"
    ) | Where-Object {{ $_ -and (Test-Path $_) }}

    $lnks = Get-ChildItem -Path $shortcutPaths -Filter "*.lnk" -Recurse -ErrorAction SilentlyContinue

    foreach ($lnk in $lnks) {{
        try {{
            $target = $sh.CreateShortcut($lnk.FullName).TargetPath
            if ($target -and $target.EndsWith(".exe") -and (Test-Path $target)) {{
                $results += $target
            }}
        }} catch {{}}
    }}

    $dirs = @({dirs_array_string}) | Where-Object {{ $_ -and (Test-Path $_) }} | Select-Object -Unique

    foreach ($dir in $dirs) {{
        $depth = if ($dir -eq "$env:LOCALAPPDATA") {{ 2 }} else {{ 3 }}
        Get-ChildItem -Path $dir -Filter "*.exe" -Depth $depth -File -ErrorAction SilentlyContinue |
            ForEach-Object {{ $results += $_.FullName }}
    }}

    $results | Select-Object -Unique
    """

    success = False
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps_script],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="ignore",
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        
        paths = []
        for line in result.stdout.splitlines():
            line = line.strip().strip('"').strip("'")
            if line and line.lower().endswith(".exe"):
                paths.append(line)
        
        seen = set()
        unique_paths = []
        for p in paths:
            p_lower = p.lower()
            if p_lower not in seen:
                seen.add(p_lower)
                unique_paths.append(p)
        
        with sqlite3.connect(DB_FILE) as conn:
            cursor = conn.cursor()
            cursor.execute("BEGIN TRANSACTION")
            cursor.execute("DELETE FROM apps_index")
            cursor.executemany(
                "INSERT OR REPLACE INTO apps_index (path, filename, last_scanned) VALUES (?, ?, datetime('now'))",
                [(p, os.path.basename(p)) for p in unique_paths]
            )
            conn.commit()
            
        print(f"Apps Index Built: {len(unique_paths)} apps indexed.")
        success = True
    except Exception as e:
        print(f"Build Apps Index Error: {e}")
        success = False
    finally:
        with indexer_lock:
            is_indexing = False
        
    if on_complete_callback:
        on_complete_callback(success)

def should_rebuild_index() -> bool:
    """インデックスを再構築すべきかどうかを判定する（前回の更新から12時間以上経過しているか、またはインデックスが空の場合）"""
    try:
        with sqlite3.connect(DB_FILE) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) FROM apps_index")
            count = cursor.fetchone()[0]
            if count == 0:
                return True
            
            cursor.execute("SELECT MAX(last_scanned) FROM apps_index")
            max_scanned = cursor.fetchone()[0]
            if not max_scanned:
                return True
            
            from datetime import datetime, timezone
            try:
                # datetime('now') は UTC なので timezone.utc を使用してパース
                dt = datetime.strptime(max_scanned, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
                now = datetime.now(timezone.utc)
                elapsed = (now - dt).total_seconds()
                # 12時間 = 43200秒
                return elapsed > 43200
            except ValueError:
                return True
    except Exception as e:
        print(f"Error checking index status: {e}")
        return True

def search_apps_from_index(query: str) -> list[str]:
    """SQLiteインデックスから大文字小文字を無視してアプリを部分一致検索する"""
    safe_query = re.sub(r'[^a-zA-Z0-9\s_\-\u3040-\u309F\u30A0-\u30FF\u4E00-\u9FFF]', '', query).strip()
    if not safe_query:
        return []
        
    try:
        with sqlite3.connect(DB_FILE) as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT path FROM apps_index WHERE filename LIKE ? ORDER BY filename ASC",
                (f"%{safe_query}%",)
            )
            return [row[0] for row in cursor.fetchall()]
    except Exception as e:
        print(f"Search Apps From Index Error: {e}")
        return []

def get_search_cache(query: str) -> list[str] | None:
    """キャッシュから検索結果を取得する"""
    try:
        with sqlite3.connect(DB_FILE) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT results FROM search_cache WHERE query = ?", (query.lower(),))
            row = cursor.fetchone()
            if row:
                return json.loads(row[0])
    except Exception as e:
        print(f"Cache Read Error: {e}")
    return None

def set_search_cache(query: str, results: list[str]):
    """検索結果をキャッシュに保存する"""
    try:
        with sqlite3.connect(DB_FILE) as conn:
            cursor = conn.cursor()
            cursor.execute(
                "INSERT OR REPLACE INTO search_cache (query, results) VALUES (?, ?)",
                (query.lower(), json.dumps(results))
            )
            conn.commit()
    except Exception as e:
        print(f"Cache Write Error: {e}")

def save_shortcut_to_db(alias: str, exe_path: str):
    """ショートカット情報をデータベースに保存（登録状態）"""
    if not alias.lower().endswith(".exe"):
        alias += ".exe"
    try:
        with sqlite3.connect(DB_FILE) as conn:
            cursor = conn.cursor()
            cursor.execute(
                "INSERT OR REPLACE INTO shortcuts (alias, exe_path, is_deleted) VALUES (?, ?, 0)",
                (alias, exe_path)
            )
            conn.commit()
    except Exception as e:
        print(f"DB Write Error: {e}")

def mark_shortcut_deleted(alias: str):
    """ショートカット情報を削除状態としてマーク"""
    if not alias.lower().endswith(".exe"):
        alias += ".exe"
    try:
        with sqlite3.connect(DB_FILE) as conn:
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE shortcuts SET is_deleted = 1 WHERE alias = ?",
                (alias,)
            )
            conn.commit()
    except Exception as e:
        print(f"DB Update Error: {e}")

def mark_shortcut_active(alias: str):
    """ショートカット情報をアクティブ（有効）としてマーク"""
    if not alias.lower().endswith(".exe"):
        alias += ".exe"
    try:
        with sqlite3.connect(DB_FILE) as conn:
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE shortcuts SET is_deleted = 0 WHERE alias = ?",
                (alias,)
            )
            conn.commit()
    except Exception as e:
        print(f"DB Update Error: {e}")

def get_active_shortcuts() -> list[tuple[str, str]]:
    """登録中の（削除されていない）ショートカット一覧を取得"""
    try:
        with sqlite3.connect(DB_FILE) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT alias, exe_path FROM shortcuts WHERE is_deleted = 0")
            return cursor.fetchall()
    except Exception as e:
        print(f"DB Read Error: {e}")
        return []

def get_deleted_shortcuts() -> list[tuple[str, str]]:
    """一括削除などで論理削除されたショートカット一覧を取得"""
    try:
        with sqlite3.connect(DB_FILE) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT alias, exe_path FROM shortcuts WHERE is_deleted = 1")
            return cursor.fetchall()
    except Exception as e:
        print(f"DB Read Error: {e}")
        return []

def get_all_shortcuts_from_db() -> list[tuple[str, str, int]]:
    """データベースからすべてのショートカット（アクティブ＆論理削除済み）を取得"""
    try:
        with sqlite3.connect(DB_FILE) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT alias, exe_path, is_deleted FROM shortcuts ORDER BY registered_at DESC")
            return cursor.fetchall()
    except Exception as e:
        print(f"DB Read Error: {e}")
        return []

def mark_all_active_deleted():
    """すべての登録中のショートカットを論理削除状態にする"""
    try:
        with sqlite3.connect(DB_FILE) as conn:
            cursor = conn.cursor()
            cursor.execute("UPDATE shortcuts SET is_deleted = 1 WHERE is_deleted = 0")
            conn.commit()
    except Exception as e:
        print(f"DB Update Error: {e}")

def mark_all_deleted_active():
    """すべての論理削除されたショートカットをアクティブ状態にする"""
    try:
        with sqlite3.connect(DB_FILE) as conn:
            cursor = conn.cursor()
            cursor.execute("UPDATE shortcuts SET is_deleted = 0 WHERE is_deleted = 1")
            conn.commit()
    except Exception as e:
        print(f"DB Update Error: {e}")

def register_app_path(alias: str, exe_path: str) -> tuple[bool, str]:
    """WindowsのHKCUレジストリにショートカット名を登録する（管理者権限不要）"""
    if not alias or not exe_path:
        return False, "すべての項目を入力してください。"
    
    if not alias.lower().endswith(".exe"):
        alias += ".exe"
        
    key_path = rf"Software\Microsoft\Windows\CurrentVersion\App Paths\{alias}"
    try:
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, key_path) as key:
            winreg.SetValueEx(key, "", 0, winreg.REG_SZ, exe_path)
            app_dir = os.path.dirname(exe_path)
            winreg.SetValueEx(key, "Path", 0, winreg.REG_SZ, app_dir)
        return True, alias
    except Exception as e:
        return False, str(e)

def unregister_app_path(alias: str) -> tuple[bool, str]:
    """WindowsのHKCUレジストリからショートカット名を削除する"""
    if not alias:
        return False, "エイリアス名が指定されていません。"
    
    if not alias.lower().endswith(".exe"):
        alias += ".exe"
        
    key_path = r"Software\Microsoft\Windows\CurrentVersion\App Paths"
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_ALL_ACCESS) as key:
            winreg.DeleteKey(key, alias)
        return True, alias
    except FileNotFoundError:
        return True, alias
    except Exception as e:
        return False, str(e)

def open_file_dialog_via_powershell() -> str:
    """PowerShell の Windows Forms を使ってネイティブファイル選択ダイアログを開く"""
    ps_script = r"""
    Add-Type -AssemblyName System.Windows.Forms
    $dialog = New-Object System.Windows.Forms.OpenFileDialog
    $dialog.Title = '対象のexeファイルを選択してください'
    $dialog.Filter = 'Executable files (*.exe)|*.exe'
    $dialog.InitialDirectory = 'C:\'
    if ($dialog.ShowDialog() -eq 'OK') {
        $dialog.FileName
    } else {
        ''
    }
    """
    result = subprocess.run(
        ["powershell", "-NoProfile", "-Command", ps_script],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="ignore",
    )
    return result.stdout.strip()

def open_folder_dialog_via_powershell() -> str:
    """PowerShell の FolderBrowserDialog を使ってネイティブフォルダ選択ダイアログを開く"""
    ps_script = r"""
    Add-Type -AssemblyName System.Windows.Forms
    $dialog = New-Object System.Windows.Forms.FolderBrowserDialog
    $dialog.Description = '検索対象のフォルダを選択してください'
    if ($dialog.ShowDialog() -eq 'OK') {
        $dialog.SelectedPath
    } else {
        ''
    }
    """
    result = subprocess.run(
        ["powershell", "-NoProfile", "-Command", ps_script],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="ignore",
    )
    return result.stdout.strip()

def search_apps_via_powershell(query: str, custom_dirs: list[str]) -> list[str]:
    """PowerShellを使用して、指定されたディレクトリからexeファイルを検索する"""
    safe_query = re.sub(r'[^a-zA-Z0-9\s_\-\u3040-\u309F\u30A0-\u30FF\u4E00-\u9FFF]', '', query).strip()
    if not safe_query:
        return []

    escaped_query = safe_query.replace("'", "''")

    # デフォルトのディレクトリ
    default_dirs = [
        "$env:ProgramFiles",
        "${env:ProgramFiles(x86)}",
        "$env:LOCALAPPDATA\\Programs",
        "$env:LOCALAPPDATA"
    ]
    
    # カスタムディレクトリを追加
    all_dirs_ps = []
    for d in default_dirs:
        all_dirs_ps.append(f'"{d}"')
    for d in custom_dirs:
        escaped_d = d.replace("'", "''")
        all_dirs_ps.append(f'"{escaped_d}"')
        
    dirs_array_string = ", ".join(all_dirs_ps)

    ps_script = f"""
    $sh = New-Object -ComObject WScript.Shell
    $results = @()

    # 1. スタートメニューとデスクトップのショートカット (.lnk) から検索
    $shortcutPaths = @(
        "$env:APPDATA\\Microsoft\\Windows\\Start Menu\\Programs",
        "$env:ProgramData\\Microsoft\\Windows\\Start Menu\\Programs",
        "$env:USERPROFILE\\Desktop",
        "C:\\Users\\Public\\Desktop"
    ) | Where-Object {{ $_ -and (Test-Path $_) }}

    $lnks = Get-ChildItem -Path $shortcutPaths -Filter "*.lnk" -Recurse -ErrorAction SilentlyContinue |
        Where-Object {{ $_.Name -like "*{escaped_query}*" }}

    foreach ($lnk in $lnks) {{
        try {{
            $target = $sh.CreateShortcut($lnk.FullName).TargetPath
            if ($target -and $target.EndsWith(".exe") -and (Test-Path $target)) {{
                $results += $target
            }}
        }} catch {{}}
    }}

    # 2. 一般的なインストールディレクトリを浅い階層で直接検索 (Depth 3)
    $dirs = @({dirs_array_string}) | Where-Object {{ $_ -and (Test-Path $_) }} | Select-Object -Unique

    foreach ($dir in $dirs) {{
        $depth = if ($dir -eq "$env:LOCALAPPDATA") {{ 2 }} else {{ 3 }}
        Get-ChildItem -Path $dir -Filter "*{escaped_query}*.exe" -Depth $depth -File -ErrorAction SilentlyContinue |
            ForEach-Object {{ $results += $_.FullName }}
    }}

    $results | Select-Object -Unique
    """

    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps_script],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="ignore",
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        
        paths = []
        for line in result.stdout.splitlines():
            line = line.strip().strip('"').strip("'")
            if line and line.lower().endswith(".exe"):
                paths.append(line)
        
        # 重複を削除（順序維持）
        seen = set()
        unique_paths = []
        for p in paths:
            p_lower = p.lower()
            if p_lower not in seen:
                seen.add(p_lower)
                unique_paths.append(p)
        
        return unique_paths
    except Exception as e:
        print(f"PowerShell Search Error: {e}")
        return []

def show_snack(page: ft.Page, message: str, bgcolor: str):
    page.show_dialog(ft.SnackBar(
        content=ft.Text(message),
        bgcolor=bgcolor,
        open=True,
    ))

def main(page: ft.Page):
    page.title = "Win + R Shortcut Wizard"
    page.window_width = 540
    page.window_height = 900
    page.window_resizable = False
    page.theme_mode = ft.ThemeMode.DARK
    page.padding = 25
    page.scroll = ft.ScrollMode.AUTO
    
    index_status = ft.Text("検索インデックス更新中...", size=10, color=ft.Colors.AMBER_400, visible=False)

    alias_input = ft.TextField(
        label="希望するショートカット名",
        hint_text="例: craft, todo, cursor",
        border_color="surfacevariant",
        focused_border_color=ft.Colors.BLUE_ACCENT,
        text_size=14,
        label_style=ft.TextStyle(color=ft.Colors.WHITE_30, size=11),
        hint_style=ft.TextStyle(color=ft.Colors.WHITE_30, size=11),
    )
    
    path_input = ft.TextField(
        label="対象アプリの絶対パス (.exe)",
        hint_text="手動で選択するか、下の検索機能を使用してください",
        border_color="surfacevariant",
        focused_border_color=ft.Colors.BLUE_ACCENT,
        text_size=12,
        expand=True,
        label_style=ft.TextStyle(color=ft.Colors.WHITE_30, size=10),
        hint_style=ft.TextStyle(color=ft.Colors.WHITE_30, size=10),
    )

    search_input = ft.TextField(
        label="アプリ名でPC内を検索",
        hint_text="例: craft, chrome, notepad",
        border_color="surfacevariant",
        focused_border_color=ft.Colors.BLUE_ACCENT,
        text_size=14,
        expand=True,
        on_submit=lambda e: handle_search(e),
        label_style=ft.TextStyle(color=ft.Colors.WHITE_30, size=11),
        hint_style=ft.TextStyle(color=ft.Colors.WHITE_30, size=11),
    )
    
    search_progress = ft.ProgressRing(visible=False, width=20, height=20, stroke_width=2)
    candidates_list = ft.ListView(expand=True, spacing=5, height=200)
    candidates_container = ft.Container(
        content=candidates_list,
        border=ft.Border.all(1, "surfacevariant"),
        border_radius=8,
        padding=10,
        visible=False,
    )

    # 検索対象パスの表示用Column
    paths_list_column = ft.Column(spacing=2)
    paths_list_container = ft.Container(
        content=paths_list_column,
        border=ft.Border.all(1, "surfacevariant"),
        border_radius=8,
        padding=10,
    )
    
    def get_env_expanded_defaults() -> list[str]:
        paths = []
        pf = os.environ.get("ProgramFiles")
        if pf: paths.append(pf)
        pf86 = os.environ.get("ProgramFiles(x86)")
        if pf86: paths.append(pf86)
        local_app = os.environ.get("LOCALAPPDATA")
        if local_app:
            paths.append(os.path.join(local_app, "Programs"))
            paths.append(local_app)
        return paths

    def refresh_paths_list_ui():
        paths_list_column.controls.clear()
        
        # デフォルトパスの表示
        defaults = get_env_expanded_defaults()
        for p in defaults:
            paths_list_column.controls.append(
                ft.Row(
                    controls=[
                        ft.Icon(ft.Icons.FOLDER_OPEN, size=12, color=ft.Colors.SECONDARY),
                        ft.Text(p, size=10, color=ft.Colors.SECONDARY, expand=True),
                        ft.Text("(デフォルト)", size=9, color=ft.Colors.WHITE_30),
                    ],
                    spacing=5,
                )
            )
            
        # カスタムパスの表示
        customs = get_custom_paths_from_db()
        for p in customs:
            def make_delete_path(path_to_del):
                return lambda _: handle_delete_custom_path(path_to_del)
                
            paths_list_column.controls.append(
                ft.Row(
                    controls=[
                        ft.Icon(ft.Icons.FOLDER, size=12, color=ft.Colors.BLUE_ACCENT),
                        ft.Text(p, size=10, color=ft.Colors.BLUE_ACCENT, expand=True),
                        ft.IconButton(
                            icon=ft.Icons.CLOSE_ROUNDED,
                            icon_size=10,
                            visual_density=ft.VisualDensity.COMPACT,
                            tooltip="この検索対象を削除",
                            on_click=make_delete_path(p),
                        ),
                    ],
                    spacing=5,
                )
            )
        page.update()

    def trigger_index_build(force=False):
        global is_indexing
        if is_indexing:
            # 既にバックグラウンドで動いている場合は新たな構築を走らせない
            return

        if not force and not should_rebuild_index():
            print("Apps index is up-to-date. Skipping build.")
            return

        index_status.visible = True
        page.update()
        
        def on_complete(success):
            index_status.visible = False
            if success:
                show_snack(page, "検索インデックスを更新しました。", ft.Colors.GREEN_700)
            else:
                show_snack(page, "検索インデックスの更新に失敗しました。", ft.Colors.RED_700)
            page.update()
            
        custom_dirs = get_custom_paths_from_db()
        threading.Thread(target=build_apps_index_in_background, args=(custom_dirs, on_complete), daemon=True).start()

    def handle_delete_custom_path(path):
        delete_custom_path_from_db(path)
        show_snack(page, f"検索対象から「{os.path.basename(path)}」を削除しました。", ft.Colors.GREEN_700)
        refresh_paths_list_ui()
        trigger_index_build(force=True)

    async def handle_add_custom_path(e):
        path = await asyncio.to_thread(open_folder_dialog_via_powershell)
        if path:
            add_custom_path_to_db(path)
            show_snack(page, f"検索対象に「{path}」を追加しました。", ft.Colors.GREEN_700)
            refresh_paths_list_ui()
            trigger_index_build(force=True)

    # ファイル選択ボタン（PowerShell の WinForms ダイアログを使用）
    async def pick_file_clicked(e):
        # 別スレッドでブロッキングな PowerShell ダイアログを開く
        path = await asyncio.to_thread(open_file_dialog_via_powershell)
        if path:
            path_input.value = path
            page.update()

    def select_candidate(path):
        path_input.value = path
        page.update()

    def run_search(query):
        search_progress.visible = True
        search_button.disabled = True
        candidates_list.controls.clear()
        candidates_container.visible = True
        candidates_list.controls.append(ft.Text("検索中...", italic=True, color=ft.Colors.SECONDARY))
        page.update()

        clean_query = re.sub(r'[^a-zA-Z0-9\s_\-\u3040-\u309F\u30A0-\u30FF\u4E00-\u9FFF]', '', query).strip()
        if not clean_query:
            search_progress.visible = False
            search_button.disabled = False
            candidates_list.controls.clear()
            candidates_list.controls.append(ft.Text("有効な検索キーワードを入力してください。", color=ft.Colors.RED_400))
            page.update()
            return

        # インデックスDBから部分一致で取得
        results = search_apps_from_index(clean_query)

        candidates_list.controls.clear()
        if not results:
            candidates_list.controls.append(
                ft.Container(
                    content=ft.Column(
                        controls=[
                            ft.Row(
                                controls=[
                                    ft.Icon(ft.Icons.INFO_OUTLINED, color=ft.Colors.RED_400, size=20),
                                    ft.Text(
                                        f"「{clean_query}」が見つかりません",
                                        color=ft.Colors.RED_400,
                                        size=14,
                                        weight=ft.FontWeight.BOLD,
                                    ),
                                ],
                                spacing=5,
                            ),
                            ft.Text(
                                "PC内に対象の.exeファイルが見つかりませんでした。\n正確なアプリ名を入力するか、上の「フォルダ追加」ボタンから検索対象のディレクトリを追加してください。",
                                size=11,
                                color=ft.Colors.SECONDARY,
                            ),
                        ],
                        spacing=8,
                    ),
                    padding=5,
                )
            )
        else:
            candidates_list.controls.append(
                ft.Text(
                    f"検索結果 ({len(results)}件): クリックして選択",
                    size=12,
                    color=ft.Colors.BLUE_ACCENT,
                    weight=ft.FontWeight.BOLD,
                )
            )
            for path in results:
                filename = os.path.basename(path)
                candidates_list.controls.append(
                    ft.ListTile(
                        leading=ft.Icon(ft.Icons.PLAY_ARROW_ROUNDED, color=ft.Colors.BLUE_ACCENT),
                        title=ft.Text(filename, size=14, weight=ft.FontWeight.W_600),
                        subtitle=ft.Text(path, size=11, color=ft.Colors.SECONDARY),
                        on_click=lambda _, p=path: select_candidate(p),
                        hover_color="surfacevariant",
                    )
                )

        search_progress.visible = False
        search_button.disabled = False
        page.update()

    def handle_search(e):
        query = search_input.value.strip()
        if not query:
            show_snack(page, "検索キーワードを入力してください。", ft.Colors.AMBER_700)
            return
        threading.Thread(target=run_search, args=(query,), daemon=True).start()

    def handle_submit(e):
        alias = alias_input.value.strip()
        path = path_input.value.strip()
        
        if not alias or not path:
            show_snack(page, "すべての項目を入力してください。", ft.Colors.AMBER_700)
            return
            
        success, message = register_app_path(alias, path)
        
        if success:
            save_shortcut_to_db(message, path)
            show_snack(
                page,
                f"成功！「Win + R」→「{message}」で起動できます。（再起動は不要です）",
                ft.Colors.GREEN_700,
            )
            alias_input.value = ""
            path_input.value = ""
            search_input.value = ""
            candidates_container.visible = False
            refresh_shortcuts_ui()
        else:
            show_snack(page, f"エラー: {message}", ft.Colors.RED_700)
        page.update()

    # チェックされたアイテムを保持するセット
    checked_items = set()

    # 登録済みショートカットを表示するためのリストビュー
    shortcuts_list = ft.ListView(expand=True, spacing=5, height=180)
    shortcuts_container = ft.Container(
        content=shortcuts_list,
        border=ft.Border.all(1, "surfacevariant"),
        border_radius=8,
        padding=10,
    )

    def refresh_shortcuts_ui():
        shortcuts_list.controls.clear()
        checked_items.clear()
        
        all_shortcuts = get_all_shortcuts_from_db()
        if not all_shortcuts:
            shortcuts_list.controls.append(
                ft.Text("登録されたショートカットはありません。", color=ft.Colors.SECONDARY, italic=True)
            )
            page.update()
            return
            
        for alias, exe_path, is_deleted in all_shortcuts:
            status_text = "有効" if is_deleted == 0 else "削除済"
            status_color = ft.Colors.GREEN_400 if is_deleted == 0 else ft.Colors.RED_400
            
            # 各項目のチェックボックス選択時のアクション
            def make_on_change(a):
                return lambda e: checked_items.add(a) if e.control.value else checked_items.discard(a)

            cb = ft.Checkbox(
                value=False,
                on_change=make_on_change(alias),
            )
            
            # 個別のアクションボタン（削除または復元）
            if is_deleted == 0:
                action_btn = ft.IconButton(
                    icon=ft.Icons.DELETE_ROUNDED,
                    icon_color=ft.Colors.RED_400,
                    tooltip="このショートカットを削除",
                    on_click=lambda _, a=alias: handle_single_delete(a),
                )
                text_style = ft.TextStyle()
            else:
                action_btn = ft.IconButton(
                    icon=ft.Icons.RESTORE_ROUNDED,
                    icon_color=ft.Colors.GREEN_400,
                    tooltip="このショートカットを復元",
                    on_click=lambda _, a=alias, p=exe_path: handle_single_restore(a, p),
                )
                text_style = ft.TextStyle(decoration=ft.TextDecoration.LINE_THROUGH, color=ft.Colors.SECONDARY)
            
            shortcuts_list.controls.append(
                ft.Row(
                    controls=[
                        cb,
                        ft.Column(
                            controls=[
                                ft.Text(alias, size=14, weight=ft.FontWeight.W_600, style=text_style),
                                ft.Text(exe_path, size=11, color=ft.Colors.SECONDARY),
                            ],
                            expand=True,
                            spacing=2,
                        ),
                        ft.Text(status_text, size=11, color=status_color),
                        action_btn,
                    ],
                    alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                )
            )
        page.update()

    def handle_single_delete(alias):
        success, msg = unregister_app_path(alias)
        if success:
            mark_shortcut_deleted(alias)
            show_snack(page, f"ショートカット「{alias}」を削除しました。", ft.Colors.GREEN_700)
        else:
            show_snack(page, f"削除エラー: {msg}", ft.Colors.RED_700)
        refresh_shortcuts_ui()

    def handle_single_restore(alias, exe_path):
        success, msg = register_app_path(alias, exe_path)
        if success:
            mark_shortcut_active(alias)
            show_snack(page, f"ショートカット「{alias}」を復元しました！", ft.Colors.GREEN_700)
        else:
            show_snack(page, f"復元エラー: {msg}", ft.Colors.RED_700)
        refresh_shortcuts_ui()

    def handle_selected_delete(e):
        if not checked_items:
            show_snack(page, "選択された項目がありません。", ft.Colors.AMBER_700)
            return
            
        success_count = 0
        errors = []
        for alias in list(checked_items):
            success, msg = unregister_app_path(alias)
            if success:
                mark_shortcut_deleted(alias)
                success_count += 1
            else:
                errors.append(f"{alias}: {msg}")
                
        if errors:
            show_snack(page, f"{success_count}件を削除しましたが、一部でエラーが発生しました。\n" + "\n".join(errors), ft.Colors.AMBER_700)
        else:
            show_snack(page, f"選択したショートカット（{success_count}件）を削除しました。", ft.Colors.GREEN_700)
        refresh_shortcuts_ui()

    def handle_selected_restore(e):
        if not checked_items:
            show_snack(page, "選択された項目がありません。", ft.Colors.AMBER_700)
            return
            
        all_shortcuts = get_all_shortcuts_from_db()
        path_map = {alias: exe_path for alias, exe_path, _ in all_shortcuts}
        
        success_count = 0
        errors = []
        for alias in list(checked_items):
            exe_path = path_map.get(alias)
            if not exe_path:
                continue
            success, msg = register_app_path(alias, exe_path)
            if success:
                mark_shortcut_active(alias)
                success_count += 1
            else:
                errors.append(f"{alias}: {msg}")
                
        if errors:
            show_snack(page, f"{success_count}件を復元しましたが、一部でエラーが発生しました。\n" + "\n".join(errors), ft.Colors.AMBER_700)
        else:
            show_snack(page, f"選択したショートカット（{success_count}件）を復元しました！", ft.Colors.GREEN_700)
        refresh_shortcuts_ui()

    def handle_bulk_delete(e):
        active_list = get_active_shortcuts()
        if not active_list:
            show_snack(page, "削除対象の登録済みショートカットがありません。", ft.Colors.AMBER_700)
            return
            
        success_count = 0
        errors = []
        for alias, exe_path in active_list:
            success, msg = unregister_app_path(alias)
            if success:
                success_count += 1
            else:
                errors.append(f"{alias}: {msg}")
        
        mark_all_active_deleted()
        
        if errors:
            show_snack(page, f"{success_count}件を削除しましたが、一部でエラーが発生しました。\n" + "\n".join(errors), ft.Colors.AMBER_700)
        else:
            show_snack(page, f"すべてのショートカット（{success_count}件）を一括削除しました。元に戻すことができます。", ft.Colors.GREEN_700)
        refresh_shortcuts_ui()

    def handle_bulk_restore(e):
        deleted_list = get_deleted_shortcuts()
        if not deleted_list:
            show_snack(page, "復元対象の削除済みショートカットがありません。", ft.Colors.AMBER_700)
            return
            
        success_count = 0
        errors = []
        for alias, exe_path in deleted_list:
            success, msg = register_app_path(alias, exe_path)
            if success:
                success_count += 1
            else:
                errors.append(f"{alias}: {msg}")
        
        mark_all_deleted_active()
        
        if errors:
            show_snack(page, f"{success_count}件を復元しましたが、一部でエラーが発生しました。\n" + "\n".join(errors), ft.Colors.AMBER_700)
        else:
            show_snack(page, f"すべてのショートカット（{success_count}件）を復元しました！", ft.Colors.GREEN_700)
        refresh_shortcuts_ui()

    search_button = ft.IconButton(
        icon=ft.Icons.SEARCH,
        icon_color=ft.Colors.BLUE_ACCENT,
        on_click=handle_search,
    )

    page.add(
        ft.Column(
            controls=[
                ft.Text("Shortcut Wizard", size=24, weight=ft.FontWeight.BOLD, color=ft.Colors.BLUE_ACCENT),
                ft.Text("Win + R から一発起動するエイリアスを作成します", size=12, color=ft.Colors.SECONDARY),
                ft.Divider(height=20, color="surfacevariant"),
                
                alias_input,
                
                ft.Row(
                    controls=[
                        path_input,
                        ft.IconButton(
                            icon=ft.Icons.FOLDER_OPEN,
                            icon_color=ft.Colors.BLUE_ACCENT,
                            on_click=pick_file_clicked,
                            tooltip="手動でファイルを選択",
                        ),
                    ],
                    alignment=ft.MainAxisAlignment.CENTER,
                ),
                
                ft.Divider(height=20, color="surfacevariant"),
                
                ft.Row(
                    controls=[
                        ft.Row(
                            controls=[
                                ft.Text("アプリの検索 (自動入力)", size=14, weight=ft.FontWeight.BOLD),
                                index_status,
                            ],
                            spacing=10,
                        ),
                        ft.Row(
                            controls=[
                                ft.IconButton(
                                    icon=ft.Icons.REFRESH_ROUNDED,
                                    icon_size=16,
                                    icon_color=ft.Colors.BLUE_ACCENT,
                                    tooltip="検索インデックスを再構築",
                                    on_click=lambda _: trigger_index_build(force=True),
                                ),
                                ft.IconButton(
                                    icon=ft.Icons.CREATE_NEW_FOLDER_ROUNDED,
                                    icon_size=16,
                                    icon_color=ft.Colors.BLUE_ACCENT,
                                    tooltip="検索対象のフォルダを追加",
                                    on_click=handle_add_custom_path,
                                ),
                            ],
                            spacing=5,
                        ),
                    ],
                    alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                ),
                paths_list_container,
                ft.Row(
                    controls=[search_input, search_progress, search_button],
                ),
                
                candidates_container,
                
                ft.Container(height=10),
                
                ft.Button(
                    content=ft.Text("ショートカットを作成", color=ft.Colors.WHITE),
                    bgcolor=ft.Colors.BLUE_ACCENT,
                    style=ft.ButtonStyle(
                        shape=ft.RoundedRectangleBorder(radius=8),
                    ),
                    height=45,
                    width=float("inf"),
                    on_click=handle_submit,
                ),
                
                ft.Divider(height=20, color="surfacevariant"),
                
                ft.Text("登録済みショートカット一覧", size=14, weight=ft.FontWeight.BOLD),
                shortcuts_container,
                
                ft.Row(
                    controls=[
                        ft.Button(
                            content=ft.Text("選択削除", color=ft.Colors.WHITE),
                            bgcolor=ft.Colors.RED_ACCENT_400,
                            style=ft.ButtonStyle(
                                shape=ft.RoundedRectangleBorder(radius=8),
                            ),
                            expand=True,
                            height=35,
                            on_click=handle_selected_delete,
                        ),
                        ft.Button(
                            content=ft.Text("選択復元", color=ft.Colors.WHITE),
                            bgcolor=ft.Colors.GREEN_ACCENT_400,
                            style=ft.ButtonStyle(
                                shape=ft.RoundedRectangleBorder(radius=8),
                            ),
                            expand=True,
                            height=35,
                            on_click=handle_selected_restore,
                        ),
                    ]
                ),
                ft.Row(
                    controls=[
                        ft.Button(
                            content=ft.Text("一括削除", color=ft.Colors.RED_400),
                            bgcolor=ft.Colors.SURFACE_CONTAINER_HIGH,
                            style=ft.ButtonStyle(
                                shape=ft.RoundedRectangleBorder(radius=8),
                            ),
                            expand=True,
                            height=35,
                            on_click=handle_bulk_delete,
                        ),
                        ft.Button(
                            content=ft.Text("一括復元", color=ft.Colors.GREEN_400),
                            bgcolor=ft.Colors.SURFACE_CONTAINER_HIGH,
                            style=ft.ButtonStyle(
                                shape=ft.RoundedRectangleBorder(radius=8),
                            ),
                            expand=True,
                            height=35,
                            on_click=handle_bulk_restore,
                        ),
                    ]
                ),
            ],
            spacing=15,
            horizontal_alignment=ft.CrossAxisAlignment.START,
        )
    )

    # 初回UI更新
    refresh_shortcuts_ui()
    refresh_paths_list_ui()
    trigger_index_build(force=False)

if __name__ == "__main__":
    init_db()
    import sys
    if "--web" in sys.argv:
        ft.app(target=main, view=None, port=8551)
    else:
        ft.run(main)