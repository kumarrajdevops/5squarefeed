from pydantic_settings import BaseSettings, SettingsConfigDict  # Application settings


class Settings(BaseSettings):
    # Application environment
    app_env: str = "local"

    # PostgreSQL configuration
    postgres_db: str = "ai_news"
    postgres_user: str = "ai_news"
    postgres_password: str = "ai_news_dev"
    postgres_host: str = "postgres"
    postgres_port: int = 5432

    # Redis configuration
    redis_url: str = "redis://redis:6379/0"

    # Daily news collection window
    news_window_hours: int = 22

    # YouTube Data API v3 credentials for the Publishing Worker (see
    # app/publishing/youtube_publisher.py). None until a real Google
    # Cloud OAuth client + one-time consent flow exist -- see
    # app/scripts/youtube_oauth_setup.py and README.md's Publishing
    # section for setup steps. Deliberately optional (not required to
    # run the rest of the pipeline) so dev/testing never needs a real
    # YouTube account.
    #
    # Two fully separate credential sets (dev vs prod) -- different
    # Google Cloud OAuth clients AND, per the user's explicit choice,
    # different destination YouTube channels. Both can be configured
    # in .env at once; youtube_environment picks which pair is
    # actually used at runtime, so switching modes never means editing
    # secrets back and forth. Reason: YouTube's API quota is tracked
    # per Google Cloud project, not per channel -- sharing one
    # credential set between dev testing and real publishing risks
    # burning the day's quota on test uploads and blocking a real
    # publish.
    youtube_environment: str = "dev"  # "dev" or "prod"

    youtube_dev_client_id: str | None = None
    youtube_dev_client_secret: str | None = None
    youtube_dev_refresh_token: str | None = None

    youtube_prod_client_id: str | None = None
    youtube_prod_client_secret: str | None = None
    youtube_prod_refresh_token: str | None = None

    @property
    def youtube_client_id(self) -> str | None:
        return self.youtube_prod_client_id if self.youtube_environment == "prod" else self.youtube_dev_client_id

    @property
    def youtube_client_secret(self) -> str | None:
        return self.youtube_prod_client_secret if self.youtube_environment == "prod" else self.youtube_dev_client_secret

    @property
    def youtube_refresh_token(self) -> str | None:
        return self.youtube_prod_refresh_token if self.youtube_environment == "prod" else self.youtube_dev_refresh_token

    @property
    def youtube_configured(self) -> bool:
        return bool(
            self.youtube_client_id
            and self.youtube_client_secret
            and self.youtube_refresh_token
        )

    # Build the PostgreSQL SQLAlchemy URL.
    # The project uses psycopg (PostgreSQL driver version 3).
    @property
    def database_url(self) -> str:
        return (
            f"postgresql+psycopg://"
            f"{self.postgres_user}:"
            f"{self.postgres_password}@"
            f"{self.postgres_host}:"
            f"{self.postgres_port}/"
            f"{self.postgres_db}"
        )

    # Load configuration from .env.
    # Environment variables override the defaults above.
    model_config = SettingsConfigDict(
        env_file=".env",
        extra="ignore",
    )


# Create the application settings object.
settings = Settings()
