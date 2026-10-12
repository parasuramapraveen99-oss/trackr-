"""/families and PUT /investors/{id}/family -- an adviser's named groups of clients.

Every route is scoped to the caller's login (app.db.families). A family that belongs to
another login answers 404, the same as one that does not exist.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status

from app.auth.dependencies import get_current_user, require_owned_investor
from app.database import get_conn
from app.db import families
from app.schemas.families import FamilyIn, FamilyOut, InvestorFamilyIn

router = APIRouter(tags=["families"])


def _not_found() -> HTTPException:
    return HTTPException(status.HTTP_404_NOT_FOUND, "No such family.")


def _duplicate(name: str) -> HTTPException:
    return HTTPException(status.HTTP_409_CONFLICT, f'You already have a family called "{name}".')


@router.get("/families", response_model=list[FamilyOut])
def list_families(
    connection: Any = Depends(get_conn),
    current_user: dict[str, Any] = Depends(get_current_user),
) -> list[dict[str, Any]]:
    return families.list_families(connection, str(current_user["id"]))


@router.post("/families", response_model=FamilyOut, status_code=status.HTTP_201_CREATED)
def create_family(
    body: FamilyIn,
    connection: Any = Depends(get_conn),
    current_user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    try:
        return families.create_family(connection, str(current_user["id"]), body.name)
    except families.DuplicateFamilyNameError as exc:
        raise _duplicate(str(exc)) from None
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from None


@router.patch("/families/{family_id}", response_model=FamilyOut)
def rename_family(
    family_id: str,
    body: FamilyIn,
    connection: Any = Depends(get_conn),
    current_user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    try:
        return families.rename_family(connection, str(current_user["id"]), family_id, body.name)
    except families.FamilyNotFoundError:
        raise _not_found() from None
    except families.DuplicateFamilyNameError as exc:
        raise _duplicate(str(exc)) from None
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from None


@router.delete("/families/{family_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_family(
    family_id: str,
    connection: Any = Depends(get_conn),
    current_user: dict[str, Any] = Depends(get_current_user),
) -> None:
    """Removes the grouping only. Its clients stay, with no family."""
    try:
        families.delete_family(connection, str(current_user["id"]), family_id)
    except families.FamilyNotFoundError:
        raise _not_found() from None


@router.put(
    "/investors/{investor_id}/family",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_owned_investor)],
)
def set_investor_family(
    investor_id: str,
    body: InvestorFamilyIn,
    connection: Any = Depends(get_conn),
    current_user: dict[str, Any] = Depends(get_current_user),
) -> None:
    try:
        families.set_investor_family(
            connection, str(current_user["id"]), investor_id, body.family_id
        )
    except families.FamilyNotFoundError:
        raise _not_found() from None
