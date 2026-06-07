import os
import re
import asyncio
import winreg
import subprocess
import threading
import flet as ft

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

def search_apps_via_powershell(query: str) -> list[str]:
    """PowerShellを使用して、指定されたディレクトリからexeファイルを検索する"""
    safe_query = re.sub(r'[^a-zA-Z0-9\s_\-\u3040-\u309F\u30A0-\u30FF\u4E00-\u9FFF]', '', query).strip()
    if not safe_query:
        return []

    escaped_query = safe_query.replace("'", "''")

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
    $dirs = @(
        "$env:ProgramFiles",
        "${{env:ProgramFiles(x86)}}",
        "$env:LOCALAPPDATA\\Programs",
        "$env:LOCALAPPDATA"
    ) | Where-Object {{ $_ -and (Test-Path $_) }} | Select-Object -Unique

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
    page.window_height = 680
    page.window_resizable = False
    page.theme_mode = ft.ThemeMode.DARK
    page.padding = 25
    page.scroll = ft.ScrollMode.AUTO
    
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

        results = search_apps_via_powershell(query)

        candidates_list.controls.clear()
        if not results:
            candidates_list.controls.append(
                ft.Text("候補が見つかりませんでした。別の名前でお試しください。", color=ft.Colors.RED_400)
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
            show_snack(page, "検索キーワードを入力してください。", ft.Colors.WARNING)
            return
        threading.Thread(target=run_search, args=(query,), daemon=True).start()

    def handle_submit(e):
        success, message = register_app_path(alias_input.value.strip(), path_input.value.strip())
        
        if success:
            show_snack(
                page,
                f"成功！「Win + R」→「{message}」で起動できます。（再起動は不要です）",
                ft.Colors.GREEN_700,
            )
            alias_input.value = ""
            path_input.value = ""
            search_input.value = ""
            candidates_container.visible = False
        else:
            show_snack(page, f"エラー: {message}", ft.Colors.RED_700)
        page.update()

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
                
                ft.Text("アプリの検索 (自動入力)", size=14, weight=ft.FontWeight.BOLD),
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
            ],
            spacing=15,
            horizontal_alignment=ft.CrossAxisAlignment.START,
        )
    )

if __name__ == "__main__":
    ft.run(main)