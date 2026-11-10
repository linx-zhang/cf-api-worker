"""Pydantic models shared by the routers."""

from __future__ import annotations

from pydantic import BaseModel, Field, field_validator, model_validator

from core.utils import normalize_email


class RegisterIn(BaseModel):
    email: str
    password: str = Field(min_length=8, max_length=128)
    invite_code: str | None = None

    @field_validator("email")
    @classmethod
    def _email(cls, value: str) -> str:
        return normalize_email(value)


class LoginIn(BaseModel):
    email: str
    password: str

    @field_validator("email")
    @classmethod
    def _email(cls, value: str) -> str:
        return normalize_email(value)


class UserOut(BaseModel):
    id: int
    email: str
    role: str
    created_at: str


class MeOut(UserOut):
    permissions: list[str] = Field(default_factory=list)


class TokenOut(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_at: int
    user: UserOut


class PostIn(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    body_md: str = Field(min_length=1)


class PostUpdateIn(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=200)
    body_md: str | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def _at_least_one_field(self) -> PostUpdateIn:
        if self.title is None and self.body_md is None:
            raise ValueError("at least one of title or body_md is required")
        return self


class PostOut(BaseModel):
    id: int
    author_id: int
    author_name: str
    title: str
    body_md: str
    created_at: str
    updated_at: str


class CommentIn(BaseModel):
    body: str = Field(min_length=1, max_length=5000)
    parent_id: int | None = None


class CommentOut(BaseModel):
    id: int
    post_id: int
    author_id: int
    author_name: str
    parent_id: int | None = None
    body: str
    created_at: str
