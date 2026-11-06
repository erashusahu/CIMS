import uuid

import pytest
from pydantic import ValidationError

from app.schemas.foundation import RegistrationIn, UserCreateIn


def registration(**updates):
    fields = {"business_name": "Acme", "business_identifier": "acme-traders", "owner_name": "A Owner", "email": "owner@example.com", "password": "  long password  ", "password_confirmation": "  long password  ", "profile": "retail"}
    fields.update(updates)
    return RegistrationIn(**fields)


def test_registration_preserves_password_spaces():
    result = registration()
    assert result.password == "  long password  "


@pytest.mark.parametrize("identifier", ["UPPER", "with space", "-leading", "double--hyphen"])
def test_business_identifier_rejects_invalid_values(identifier):
    with pytest.raises(ValidationError):
        registration(business_identifier=identifier)


def test_registration_password_confirmation_must_match():
    with pytest.raises(ValidationError):
        registration(password_confirmation="different password")


def test_selected_scope_requires_at_least_one_location():
    with pytest.raises(ValidationError):
        UserCreateIn(full_name="Staff", email="staff@example.com", role="Viewer", all_locations=False, location_ids=[])


def test_all_locations_cannot_also_select_specific_locations():
    with pytest.raises(ValidationError):
        UserCreateIn(full_name="Staff", email="staff@example.com", role="Viewer", all_locations=True, location_ids=[uuid.uuid4()])
