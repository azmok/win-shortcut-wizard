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
