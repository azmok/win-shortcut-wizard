# Win + R Shortcut Wizard

A Windows GUI utility built with Flet to easily register custom application aliases in the Windows Registry (`HKCU\Software\Microsoft\Windows\CurrentVersion\App Paths`). This allows you to launch your favorite apps instantly via the `Win + R` Run dialog without requiring administrator privileges.

## Features

- **Instant Alias Registration**: Map any custom alias (e.g., `craft`) to an `C:\Users\genta\AppData\Local\Programs\Craft\Craft.exe` file.
- **Fast Local Search**: Instantly find application paths on your PC. Optimized for speed by searching start menu/desktop shortcuts and shallow installation directories.
- **Native File Picker**: Select `.exe` files manually using the native Windows file dialog.
- **No Admin Privileges Needed**: Modifies the `HKEY_CURRENT_USER` registry hive so it doesn't prompt for UAC (User Account Control).

## Requirements

- Windows OS
- Python 3.9+

## Getting Started

### Using `uv` (Recommended)

If you have [uv](https://github.com/astral-sh/uv) installed:

```bash
uv run python main.py
```

### Using standard Python

1. Clone the repository and navigate into the directory.
2. Install the dependencies:
   ```bash
   pip install flet
   ```
3. Run the application:
   ```bash
   python main.py
   ```

## License

This project is licensed under the MIT License.
