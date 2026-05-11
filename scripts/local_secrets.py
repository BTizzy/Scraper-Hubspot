"""Local secret loading for ignored repo-root .env files.

This avoids hardcoding secrets in tracked Python modules while keeping
developer-local configuration simple.
"""
import os


_LOADED = False


def load_local_env():
    """Load KEY=VALUE pairs from ../.env into os.environ once."""
    global _LOADED
    if _LOADED:
        return

    env_path = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '.env'))
    if os.path.exists(env_path):
        try:
            with open(env_path, encoding='utf-8') as f:
                for raw_line in f:
                    line = raw_line.strip()
                    if not line or line.startswith('#') or '=' not in line:
                        continue
                    key, value = line.split('=', 1)
                    key = key.strip()
                    value = value.strip().strip('"').strip("'")
                    if key and key not in os.environ:
                        os.environ[key] = value
        except Exception:
            pass
    _LOADED = True
