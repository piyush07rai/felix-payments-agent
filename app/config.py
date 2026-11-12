import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

# Load .env file into os.environ if present
env_file_path = BASE_DIR / ".env"
if env_file_path.exists():
    try:
        with open(env_file_path, "r", encoding="utf-8") as _f:
            for _line in _f:
                _line = _line.strip()
                if _line and not _line.startswith("#") and "=" in _line:
                    _k, _v = _line.split("=", 1)
                    _k, _v = _k.strip(), _v.strip()
                    if _k and _k not in os.environ:
                        os.environ[_k] = _v
    except Exception:
        pass

try:
    from pydantic_settings import BaseSettings

    class Settings(BaseSettings):
        """Application configuration settings loaded from environment or .env file."""
        APP_NAME: str = "Payments Decline Analyst Agent"
        APP_ENV: str = "development"
        APP_HOST: str = "0.0.0.0"
        APP_PORT: int = 8000
        LOG_LEVEL: str = "INFO"

        # Google Gemini Configuration
        GEMINI_API_KEY: str = ""
        GEMINI_MODEL: str = "gemini-3.8-flash"

        # OpenAI ChatGPT Configuration
        OPENAI_API_KEY: str = ""
        OPENAI_MODEL: str = "gpt-4o-mini"

        # Multi-Model Routing & Resiliency
        ACTIVE_PROVIDER: str = "gemini"  # "gemini" | "openai" | "fallback"
        ENABLE_AUTO_FAILOVER: bool = True
        LLM_TIMEOUT_SECONDS: float = 18.0

        # Database Configuration
        DATABASE_PATH: str = str(BASE_DIR / "data" / "declines.db")

        # Slack App Integration Configuration
        SLACK_APP_ID: str = ""
        SLACK_BOT_TOKEN: str = ""
        SLACK_SIGNING_SECRET: str = ""
        SLACK_BOT_USER_ID: str = "U_FELIX"
        SLACK_WORKSPACE_ID: str = ""
        SLACK_WORKSPACE_NAME: str = "Enterprise Slack Workspace"
        SLACK_ENABLED: bool = True

        # Microsoft Teams Bot Integration Configuration
        TEAMS_APP_ID: str = ""
        TEAMS_APP_PASSWORD: str = ""
        TEAMS_TENANT_ID: str = ""
        TEAMS_ORGANIZATION_NAME: str = "Enterprise Microsoft 365 Tenant"
        TEAMS_ENABLED: bool = True

        # Enterprise Security & Access Control Configuration (Phase 6)
        ADMIN_API_KEY: str = "felix-admin-key-2026"
        ADMIN_USER_IDS: str = "admin,U_ADMIN,admin@enterprise.com"

        class Config:
            env_file = ".env"
            env_file_encoding = "utf-8"
            extra = "ignore"

    settings = Settings()

except ImportError:
    # Fallback when pydantic_settings is not yet installed in local python environment
    class FallbackSettings:
        APP_NAME: str = os.getenv("APP_NAME", "Payments Decline Analyst Agent")
        APP_ENV: str = os.getenv("APP_ENV", "development")
        APP_HOST: str = os.getenv("APP_HOST", "0.0.0.0")
        APP_PORT: int = int(os.getenv("APP_PORT", "8000"))
        LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO")

        GEMINI_API_KEY: str = os.getenv("GEMINI_API_KEY", "")
        GEMINI_MODEL: str = os.getenv("GEMINI_MODEL", "gemini-3.8-flash")

        OPENAI_API_KEY: str = os.getenv("OPENAI_API_KEY", "")
        OPENAI_MODEL: str = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
        ACTIVE_PROVIDER: str = os.getenv("ACTIVE_PROVIDER", "gemini")
        ENABLE_AUTO_FAILOVER: bool = os.getenv("ENABLE_AUTO_FAILOVER", "true").lower() in ("true", "1", "yes")
        LLM_TIMEOUT_SECONDS: float = float(os.getenv("LLM_TIMEOUT_SECONDS", "18.0"))

        DATABASE_PATH: str = os.getenv("DATABASE_PATH", str(BASE_DIR / "data" / "declines.db"))

        SLACK_APP_ID: str = os.getenv("SLACK_APP_ID", "")
        SLACK_BOT_TOKEN: str = os.getenv("SLACK_BOT_TOKEN", "")
        SLACK_SIGNING_SECRET: str = os.getenv("SLACK_SIGNING_SECRET", "")
        SLACK_BOT_USER_ID: str = os.getenv("SLACK_BOT_USER_ID", "U_FELIX")
        SLACK_WORKSPACE_ID: str = os.getenv("SLACK_WORKSPACE_ID", "")
        SLACK_WORKSPACE_NAME: str = os.getenv("SLACK_WORKSPACE_NAME", "Enterprise Slack Workspace")
        SLACK_ENABLED: bool = os.getenv("SLACK_ENABLED", "true").lower() in ("true", "1", "yes")

        TEAMS_APP_ID: str = os.getenv("TEAMS_APP_ID", "")
        TEAMS_APP_PASSWORD: str = os.getenv("TEAMS_APP_PASSWORD", "")
        TEAMS_TENANT_ID: str = os.getenv("TEAMS_TENANT_ID", "")
        TEAMS_ORGANIZATION_NAME: str = os.getenv("TEAMS_ORGANIZATION_NAME", "Enterprise Microsoft 365 Tenant")
        TEAMS_ENABLED: bool = os.getenv("TEAMS_ENABLED", "true").lower() in ("true", "1", "yes")

        # Enterprise Security & Access Control Configuration (Phase 6)
        ADMIN_API_KEY: str = os.getenv("ADMIN_API_KEY", "felix-admin-key-2026")
        ADMIN_USER_IDS: str = os.getenv("ADMIN_USER_IDS", "admin,U_ADMIN,admin@enterprise.com")

    settings = FallbackSettings()  # type: ignore


def save_settings_to_env(new_values: dict) -> None:
    """Persist updated settings to .env file and in-memory settings object."""
    # 1. Update in-memory settings object and os.environ
    for k, v in new_values.items():
        if hasattr(settings, k):
            setattr(settings, k, v)
        os.environ[k] = str(v)

    # 2. Safely read and update .env file
    env_lines = []
    keys_written = set()
    if env_file_path.exists():
        try:
            with open(env_file_path, "r", encoding="utf-8") as f:
                for line in f:
                    stripped = line.strip()
                    if stripped and not stripped.startswith("#") and "=" in stripped:
                        key, _ = stripped.split("=", 1)
                        key = key.strip()
                        if key in new_values:
                            env_lines.append(f"{key}={new_values[key]}\n")
                            keys_written.add(key)
                            continue
                    env_lines.append(line)
        except Exception:
            pass

    for k, v in new_values.items():
        if k not in keys_written:
            env_lines.append(f"{k}={v}\n")

    try:
        with open(env_file_path, "w", encoding="utf-8") as f:
            f.writelines(env_lines)
    except Exception:
        pass
