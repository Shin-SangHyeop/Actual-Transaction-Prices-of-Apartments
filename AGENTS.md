# Repository Guidelines

## Project Structure & Module Organization

The application is a small Python project. `app.py` contains the main application and data-fetching logic; `messaging.py` handles SMS/LMS integration; and `run.py` is the launch entry point. Automated tests live in `tests/` (`tests/test_app.py`). `docs/` contains API research. Windows startup helpers are `install_startup.ps1` and `uninstall_startup.ps1`; `앱_실행.bat` is a Windows launcher. Runtime configuration is stored in `settings.conf` and `user_settings.yaml`; keep personal API keys and apartment settings out of source control.

## Build, Test, and Development Commands

Use Python 3.10 or later. Install the sole declared dependency and launch from the repository root:

```powershell
python -m pip install -r requirements.txt
python run.py
```

Run the unit suite and syntax checks before submitting changes:

```powershell
python -m unittest discover -s tests -v
python -m py_compile app.py messaging.py run.py tests/test_app.py
```

`python verify_live_accuracy.py` compares fetched records with the public API and requires network access and configured credentials; use it only when live verification is needed.

## Coding Style & Naming Conventions

Follow the existing Python style: four spaces per indentation level, `snake_case` for functions and variables, and `UPPER_SNAKE_CASE` for module constants. Keep API access and message delivery separable from UI behavior so they can be tested with mocked responses. Use UTF-8 for source, documentation, and configuration examples.

## Testing Guidelines

Tests use Python’s standard `unittest` framework. Add or update tests in `tests/test_*.py`, with descriptive `test_...` method names. Prefer temporary files and mocked network responses; routine tests should not send messages or require live API access. Run the unit suite and compile check above after changes.

## Commit & Pull Request Guidelines

The repository currently has no Git commits, so no project-specific commit convention is established. Use short imperative commit subjects, for example `Handle empty API responses`. Pull requests should explain the user-visible change, note configuration impacts, link related issues when available, and include screenshots for UI changes. State which checks you ran and whether any live API validation was performed.

## Security & Configuration Tips

Do not commit API keys, SOLAPI credentials, recipient phone numbers, or personal configuration. Keep secrets in local configuration and avoid including them in logs, screenshots, or error messages.
