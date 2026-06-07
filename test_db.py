import os
import sqlite3
import winreg
from main import (
    init_db,
    get_search_cache,
    set_search_cache,
    save_shortcut_to_db,
    get_active_shortcuts,
    get_deleted_shortcuts,
    mark_all_active_deleted,
    mark_all_deleted_active,
    register_app_path,
    unregister_app_path,
    add_custom_path_to_db,
    delete_custom_path_from_db,
    get_custom_paths_from_db,
    search_apps_from_index,
    build_apps_index_in_background,
    should_rebuild_index,
    DB_FILE
)

def run_tests():
    print("Starting tests...")
    
    # 1. データベースの初期化
    init_db()
    assert os.path.exists(DB_FILE), "Database file was not created"
    print("[PASS] init_db")

    # 2. キャッシュのテスト
    test_query = "__test_query__"
    test_results = ["C:\\path1.exe", "C:\\path2.exe"]
    
    # キャッシュ書き込み
    set_search_cache(test_query, test_results)
    
    # キャッシュ読み込み
    cached = get_search_cache(test_query)
    assert cached == test_results, f"Cache mismatch: expected {test_results}, got {cached}"
    
    # 大文字・小文字の正規化チェック
    cached_upper = get_search_cache(test_query.upper())
    assert cached_upper == test_results, "Cache key normalization failed"
    print("[PASS] search_cache read/write & normalization")

    # キャッシュとカスタムディレクトリの連動テスト
    clean_query = "notepad"
    custom_dirs_1 = []
    custom_dirs_2 = ["C:\\CustomDir"]
    
    cache_key_1 = f"{clean_query}:{','.join(sorted(custom_dirs_1))}"
    cache_key_2 = f"{clean_query}:{','.join(sorted(custom_dirs_2))}"
    
    results_1 = ["C:\\Windows\\notepad.exe"]
    results_2 = ["C:\\Windows\\notepad.exe", "C:\\CustomDir\\notepad.exe"]
    
    set_search_cache(cache_key_1, results_1)
    set_search_cache(cache_key_2, results_2)
    
    assert get_search_cache(cache_key_1) == results_1, "Cache key 1 mismatch"
    assert get_search_cache(cache_key_2) == results_2, "Cache key 2 mismatch"
    
    # クリーンアップ
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM search_cache WHERE query IN (?, ?)", (cache_key_1.lower(), cache_key_2.lower()))
        conn.commit()
    print("[PASS] cache key separation by custom directories")

    # 2.5 カスタムパスのテスト
    print("Testing custom paths...")
    test_custom_path_1 = "C:\\__test_custom_path_1__"
    test_custom_path_2 = "C:\\__test_custom_path_2__"
    
    delete_custom_path_from_db(test_custom_path_1)
    delete_custom_path_from_db(test_custom_path_2)
    
    # 追加
    add_custom_path_to_db(test_custom_path_1)
    add_custom_path_to_db(test_custom_path_2)
    
    # 取得と検証
    customs = get_custom_paths_from_db()
    assert test_custom_path_1 in customs, "test_custom_path_1 was not added"
    assert test_custom_path_2 in customs, "test_custom_path_2 was not added"
    
    # 削除と検証
    delete_custom_path_from_db(test_custom_path_1)
    customs_after = get_custom_paths_from_db()
    assert test_custom_path_1 not in customs_after, "test_custom_path_1 was not deleted"
    assert test_custom_path_2 in customs_after, "test_custom_path_2 should still exist"
    
    # クリーンアップ
    delete_custom_path_from_db(test_custom_path_2)
    customs_final = get_custom_paths_from_db()
    assert test_custom_path_2 not in customs_final, "test_custom_path_2 cleanup failed"
    print("[PASS] custom_paths add/delete/get")

    # 2.8 アプリインデックスのテスト
    print("Testing apps_index (search indexer)...")
    test_exe_path_1 = "C:\\Windows\\System32\\__test_notepad__.exe"
    test_exe_path_2 = "C:\\Program Files\\__test_chrome__.exe"
    test_exe_path_3 = "C:\\Custom\\__test_cursor__.exe"
    
    # ダミーデータをインデックスDBへ直接挿入
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM apps_index WHERE path IN (?, ?, ?)", (test_exe_path_1, test_exe_path_2, test_exe_path_3))
        cursor.execute("INSERT OR REPLACE INTO apps_index (path, filename) VALUES (?, ?)", (test_exe_path_1, "__test_notepad__.exe"))
        cursor.execute("INSERT OR REPLACE INTO apps_index (path, filename) VALUES (?, ?)", (test_exe_path_2, "__test_chrome__.exe"))
        cursor.execute("INSERT OR REPLACE INTO apps_index (path, filename) VALUES (?, ?)", (test_exe_path_3, "__test_cursor__.exe"))
        conn.commit()
        
    # 部分一致での検索テスト
    results_note = search_apps_from_index("note")
    assert test_exe_path_1 in results_note, f"Expected {test_exe_path_1} in search results for 'note'"
    
    # 大文字小文字を無視した検索テスト
    results_chrome = search_apps_from_index("CHROME")
    assert test_exe_path_2 in results_chrome, f"Expected {test_exe_path_2} in search results for 'CHROME'"
    
    # 複数キーワードや無関係なクエリでの除外テスト
    results_none = search_apps_from_index("nonexistent_app")
    assert len(results_none) == 0, "Expected empty results for nonexistent query"
    
    # 2.8.1 should_rebuild_index の検証
    # 一度全て削除して空の状態にする
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM apps_index WHERE path IN (?, ?, ?)", (test_exe_path_1, test_exe_path_2, test_exe_path_3))
        # 既存のデータも一旦退避するか、テスト用に空にする。
        # ここでは退避せずに全削除し、後で復元するのは大変なので、テスト専用に temporary に確認する。
        # ただし、すでに本物のデータがある可能性があるので、一度本物のデータを取得して退避する。
        cursor.execute("SELECT path, filename, last_scanned FROM apps_index")
        backup_data = cursor.fetchall()
        cursor.execute("DELETE FROM apps_index")
        conn.commit()

    try:
        # 空の状態の検証
        assert should_rebuild_index() is True, "Expected True when apps_index is empty"

        # 新しいレコード（現在時刻）を追加して検証
        with sqlite3.connect(DB_FILE) as conn:
            cursor = conn.cursor()
            cursor.execute("INSERT INTO apps_index (path, filename, last_scanned) VALUES (?, ?, datetime('now'))", (test_exe_path_1, "__test_notepad__.exe"))
            conn.commit()
        assert should_rebuild_index() is False, "Expected False when index is fresh (less than 12h)"

        # 13時間前のレコードに更新して検証
        with sqlite3.connect(DB_FILE) as conn:
            cursor = conn.cursor()
            cursor.execute("UPDATE apps_index SET last_scanned = datetime('now', '-13 hours') WHERE path = ?", (test_exe_path_1,))
            conn.commit()
        assert should_rebuild_index() is True, "Expected True when index is old (more than 12h)"
    finally:
        # テストデータのクリーンアップと退避データの復元
        with sqlite3.connect(DB_FILE) as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM apps_index")
            if backup_data:
                cursor.executemany(
                    "INSERT INTO apps_index (path, filename, last_scanned) VALUES (?, ?, ?)",
                    backup_data
                )
            conn.commit()

    print("[PASS] apps_index search & lookup (including should_rebuild_index)")

    # 3. ショートカットのテスト
    test_alias_1 = "__test_shortcut_1.exe"
    test_alias_2 = "__test_shortcut_2.exe"
    test_path_1 = "C:\\Windows\\System32\\notepad.exe"
    test_path_2 = "C:\\Windows\\System32\\cmd.exe"
    
    # テスト対象を初期化（前回のゴミが残っていたら削除）
    unregister_app_path(test_alias_1)
    unregister_app_path(test_alias_2)
    
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM shortcuts WHERE alias IN (?, ?)", (test_alias_1, test_alias_2))
        conn.commit()

    # レジストリとDBへ登録
    success1, msg1 = register_app_path(test_alias_1, test_path_1)
    assert success1, f"Failed to register test_alias_1: {msg1}"
    save_shortcut_to_db(msg1, test_path_1)
    
    success2, msg2 = register_app_path(test_alias_2, test_path_2)
    assert success2, f"Failed to register test_alias_2: {msg2}"
    save_shortcut_to_db(msg2, test_path_2)
    
    # DBの登録状態チェック
    active_shortcuts = get_active_shortcuts()
    aliases = [item[0] for item in active_shortcuts]
    assert test_alias_1 in aliases, "test_alias_1 not found in active shortcuts"
    assert test_alias_2 in aliases, "test_alias_2 not found in active shortcuts"
    
    # get_all_shortcuts_from_db のテスト
    from main import get_all_shortcuts_from_db, mark_shortcut_active, mark_shortcut_deleted
    all_shortcuts = get_all_shortcuts_from_db()
    all_aliases = [item[0] for item in all_shortcuts]
    assert test_alias_1 in all_aliases, "test_alias_1 not found in all shortcuts list"
    assert test_alias_2 in all_aliases, "test_alias_2 not found in all shortcuts list"
    print("[PASS] get_all_shortcuts_from_db")

    # 個別論理削除と復元のテスト
    mark_shortcut_deleted(test_alias_1)
    all_shortcuts = get_all_shortcuts_from_db()
    status_map = {item[0]: item[2] for item in all_shortcuts}
    assert status_map[test_alias_1] == 1, "test_alias_1 was not logically deleted"
    
    mark_shortcut_active(test_alias_1)
    all_shortcuts = get_all_shortcuts_from_db()
    status_map = {item[0]: item[2] for item in all_shortcuts}
    assert status_map[test_alias_1] == 0, "test_alias_1 was not logically activated"
    print("[PASS] individual logical delete & activate")
    print("[PASS] shortcut registration (Registry + DB)")

    # レジストリにキーが存在するか確認
    def key_exists(alias):
        key_path = rf"Software\Microsoft\Windows\CurrentVersion\App Paths\{alias}"
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_READ) as key:
                val, _ = winreg.QueryValueEx(key, "")
                return val
        except FileNotFoundError:
            return None

    assert key_exists(test_alias_1) == test_path_1, "Registry check failed for test_alias_1"
    assert key_exists(test_alias_2) == test_path_2, "Registry check failed for test_alias_2"
    print("[PASS] registry verification of registered shortcuts")

    # 4. 一括削除のシミュレーション
    active_list = get_active_shortcuts()
    # テスト用のみを対象に削除
    test_active = [item for item in active_list if item[0] in (test_alias_1, test_alias_2)]
    
    for alias, exe_path in test_active:
        success, msg = unregister_app_path(alias)
        assert success, f"Failed to unregister {alias}: {msg}"
    
    mark_all_active_deleted()
    
    # レジストリから消えているか検証
    assert key_exists(test_alias_1) is None, "test_alias_1 was not deleted from registry"
    assert key_exists(test_alias_2) is None, "test_alias_2 was not deleted from registry"
    
    # DB上でdeletedになっているか検証
    active_shortcuts_after = get_active_shortcuts()
    aliases_after = [item[0] for item in active_shortcuts_after]
    assert test_alias_1 not in aliases_after, "test_alias_1 still active in DB"
    
    deleted_shortcuts = get_deleted_shortcuts()
    deleted_aliases = [item[0] for item in deleted_shortcuts]
    assert test_alias_1 in deleted_aliases, "test_alias_1 not in deleted shortcuts list"
    assert test_alias_2 in deleted_aliases, "test_alias_2 not in deleted shortcuts list"
    print("[PASS] bulk delete simulation & logical deletion status")

    # 5. 一括復元のシミュレーション
    deleted_list = get_deleted_shortcuts()
    test_deleted = [item for item in deleted_list if item[0] in (test_alias_1, test_alias_2)]
    
    for alias, exe_path in test_deleted:
        success, msg = register_app_path(alias, exe_path)
        assert success, f"Failed to restore {alias}: {msg}"
        
    mark_all_deleted_active()
    
    # レジストリに復活しているか検証
    assert key_exists(test_alias_1) == test_path_1, "Registry restore check failed for test_alias_1"
    assert key_exists(test_alias_2) == test_path_2, "Registry restore check failed for test_alias_2"
    
    # DB上でactiveに戻っているか検証
    active_shortcuts_final = get_active_shortcuts()
    aliases_final = [item[0] for item in active_shortcuts_final]
    assert test_alias_1 in aliases_final, "test_alias_1 did not return to active list in DB"
    assert test_alias_2 in aliases_final, "test_alias_2 did not return to active list in DB"
    print("[PASS] bulk restore simulation & state restoration")

    # 後片付け（クリーンアップ）
    unregister_app_path(test_alias_1)
    unregister_app_path(test_alias_2)
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM shortcuts WHERE alias IN (?, ?)", (test_alias_1, test_alias_2))
        cursor.execute("DELETE FROM search_cache WHERE query = ?", (test_query,))
        conn.commit()
    print("[PASS] cleanup test data")
    print("\nAll tests passed successfully!")

if __name__ == "__main__":
    run_tests()
