import re
import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator, model_validator


class RegistrationIn(BaseModel):
    business_name: str = Field(min_length=1, max_length=200)
    business_identifier: str = Field(min_length=3, max_length=63)
    owner_name: str = Field(min_length=1, max_length=200)
    email: EmailStr
    password: str = Field(min_length=12, max_length=128)
    password_confirmation: str = Field(min_length=12, max_length=128)
    profile: Literal["retail", "wholesale"]

    @field_validator("business_identifier")
    @classmethod
    def validate_identifier(cls, value: str) -> str:
        if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", value):
            raise ValueError("Use lowercase letters, numbers, and single hyphens")
        return value

    @model_validator(mode="after")
    def matching_passwords(self):
        if self.password != self.password_confirmation:
            raise ValueError("Passwords do not match")
        return self


class LoginIn(BaseModel):
    business_identifier: str = Field(min_length=3, max_length=63)
    email: EmailStr
    password: str = Field(min_length=1, max_length=128)


class ChangePasswordIn(BaseModel):
    new_password: str = Field(min_length=12, max_length=128)


class AuthOut(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int
    password_change_required: bool


class IdentityOut(BaseModel):
    id: uuid.UUID
    full_name: str
    email: EmailStr
    role: str
    all_locations: bool
    password_change_required: bool
    business_id: uuid.UUID
    business_name: str
    business_identifier: str


class BusinessOut(BaseModel):
    id: uuid.UUID
    identifier: str
    name: str
    profile: str
    currency: str
    country: str
    financial_year_start_month: int
    timezone: str


class BusinessPatch(BaseModel):
    name: str = Field(min_length=1, max_length=200)


class RoleOut(BaseModel):
    name: str


class LocationIn(BaseModel):
    code: str = Field(min_length=1, max_length=40)
    name: str = Field(min_length=1, max_length=150)
    type: Literal["warehouse", "godown", "store", "office"]
    address: str | None = Field(default=None, max_length=2000)
    city: str | None = Field(default=None, max_length=100)
    state_code: str | None = Field(default=None, max_length=3)
    pin_code: str | None = Field(default=None, max_length=10)
    active: bool = True


class LocationOut(LocationIn):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    created_at: datetime


class UserCreateIn(BaseModel):
    full_name: str = Field(min_length=1, max_length=200)
    email: EmailStr
    role: str
    all_locations: bool
    location_ids: list[uuid.UUID] = Field(default_factory=list)

    @model_validator(mode="after")
    def scope_is_valid(self):
        if self.all_locations and self.location_ids:
            raise ValueError("All-location access cannot include selected locations")
        if not self.all_locations and not self.location_ids:
            raise ValueError("Select at least one location or choose all locations")
        if len(set(self.location_ids)) != len(self.location_ids):
            raise ValueError("Duplicate locations are not allowed")
        return self


class UserPatchIn(BaseModel):
    full_name: str | None = Field(default=None, min_length=1, max_length=200)
    role: str | None = None
    all_locations: bool | None = None
    location_ids: list[uuid.UUID] | None = None
    suspended: bool | None = None


class StaffOut(BaseModel):
    id: uuid.UUID
    full_name: str
    email: EmailStr
    role: str
    all_locations: bool
    location_ids: list[uuid.UUID]
    suspended: bool
    password_change_required: bool


class StaffCreatedOut(StaffOut):
    temporary_password: str


class Page(BaseModel):
    items: list
    total: int
    page: int
    page_size: int
