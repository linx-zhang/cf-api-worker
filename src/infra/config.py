"""Application configuration, read from the Worker environment (env / .dev.vars)."""

from __future__ import annotations

from dataclasses import dataclass

DEFAULT_JWT_EXP_MINUTES = 60 * 24 * 7


def env_value(env, name: str, default=None):
    """Read a binding/secret from the Workers env, tolerant of missing keys."""
    try:
        value = getattr(env, name)
    except AttributeError:
        return default
    return default if value is None else value


@dataclass(frozen=True)
class Settings:
    jwt_secret: str
    jwt_algorithm: str = "HS256"
    jwt_exp_minutes: int = DEFAULT_JWT_EXP_MINUTES

    @classmethod
    def from_env(cls, env) -> Settings:
        secret = env_value(env, "JWT_SECRET")
        if not secret:
            raise RuntimeError("JWT_SECRET is not configured")
        return cls(
            jwt_secret=str(secret),
            jwt_algorithm=str(env_value(env, "JWT_ALGORITHM", cls.jwt_algorithm)),
            jwt_exp_minutes=int(
                env_value(env, "JWT_EXP_MINUTES", DEFAULT_JWT_EXP_MINUTES)
            ),
        )
