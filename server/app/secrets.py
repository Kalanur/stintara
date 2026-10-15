"""Read deployment secrets without including their values in diagnostics."""
import os
from pathlib import Path


def read_secret(env_name: str, file_env_name: str, default: str = "") -> str:
    if file_env_name not in os.environ:
        return os.environ.get(env_name, default)
    filename = os.environ[file_env_name]
    if not filename:
        raise ValueError(f"{file_env_name} must name a readable UTF-8 secret file")
    try:
        path = Path(filename)
        if path.is_dir():
            raise IsADirectoryError
        # Decode bytes explicitly: preserve embedded CR/LF and all whitespace.
        value = path.read_bytes().decode("utf-8").rstrip("\r\n")
    except FileNotFoundError:
        raise ValueError(f"{file_env_name}: secret file does not exist") from None
    except IsADirectoryError:
        raise ValueError(f"{file_env_name}: expected a file, not a directory") from None
    except (OSError, UnicodeError, ValueError):
        raise ValueError(f"{file_env_name}: cannot read a UTF-8 secret file") from None
    if not value:
        raise ValueError(f"{file_env_name}: secret file is empty")
    return value
