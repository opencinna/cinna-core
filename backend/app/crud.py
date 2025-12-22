import uuid
from typing import Any
import json

from sqlmodel import Session, select

from app.core.security import get_password_hash, verify_password, encrypt_field, decrypt_field
from app.models import (
    Agent,
    AgentCreate,
    AgentCredentialLink,
    Item,
    ItemCreate,
    User,
    UserCreate,
    UserUpdate,
    Credential,
    CredentialCreate,
    CredentialUpdate,
)


def create_user(*, session: Session, user_create: UserCreate) -> User:
    db_obj = User.model_validate(
        user_create, update={"hashed_password": get_password_hash(user_create.password)}
    )
    session.add(db_obj)
    session.commit()
    session.refresh(db_obj)
    return db_obj


def update_user(*, session: Session, db_user: User, user_in: UserUpdate) -> Any:
    user_data = user_in.model_dump(exclude_unset=True)
    extra_data = {}
    if "password" in user_data:
        password = user_data["password"]
        hashed_password = get_password_hash(password)
        extra_data["hashed_password"] = hashed_password
    db_user.sqlmodel_update(user_data, update=extra_data)
    session.add(db_user)
    session.commit()
    session.refresh(db_user)
    return db_user


def get_user_by_email(*, session: Session, email: str) -> User | None:
    statement = select(User).where(User.email == email)
    session_user = session.exec(statement).first()
    return session_user


def authenticate(*, session: Session, email: str, password: str) -> User | None:
    db_user = get_user_by_email(session=session, email=email)
    if not db_user:
        return None
    # Check if user has a password set (OAuth-only users don't have passwords)
    if not db_user.hashed_password:
        return None
    if not verify_password(password, db_user.hashed_password):
        return None
    return db_user


def get_user_by_google_id(*, session: Session, google_id: str) -> User | None:
    """Get user by Google ID."""
    statement = select(User).where(User.google_id == google_id)
    return session.exec(statement).first()


def create_user_from_google(
    *, session: Session, email: str, google_id: str, full_name: str | None
) -> User:
    """Create user from Google OAuth (no password)."""
    db_obj = User(
        email=email,
        google_id=google_id,
        full_name=full_name,
        hashed_password=None,
        is_active=True,
        is_superuser=False,
    )
    session.add(db_obj)
    session.commit()
    session.refresh(db_obj)
    return db_obj


def link_google_account(*, session: Session, user: User, google_id: str) -> User:
    """Link Google account to existing user."""
    user.google_id = google_id
    session.add(user)
    session.commit()
    session.refresh(user)
    return user


def unlink_google_account(*, session: Session, user: User) -> User:
    """Unlink Google account from user."""
    user.google_id = None
    session.add(user)
    session.commit()
    session.refresh(user)
    return user


def create_item(*, session: Session, item_in: ItemCreate, owner_id: uuid.UUID) -> Item:
    db_item = Item.model_validate(item_in, update={"owner_id": owner_id})
    session.add(db_item)
    session.commit()
    session.refresh(db_item)
    return db_item


def create_agent(*, session: Session, agent_in: AgentCreate, owner_id: uuid.UUID) -> Agent:
    db_agent = Agent.model_validate(agent_in, update={"owner_id": owner_id})
    session.add(db_agent)
    session.commit()
    session.refresh(db_agent)
    return db_agent


def create_credential(
    *, session: Session, credential_in: CredentialCreate, owner_id: uuid.UUID
) -> Credential:
    """Create a new credential with encrypted data."""
    # Encrypt the credential_data (use empty dict if None)
    credential_data = credential_in.credential_data if credential_in.credential_data is not None else {}
    credential_data_json = json.dumps(credential_data)
    encrypted_data = encrypt_field(credential_data_json)

    # Create credential without credential_data field
    db_credential = Credential(
        name=credential_in.name,
        type=credential_in.type,
        notes=credential_in.notes,
        encrypted_data=encrypted_data,
        owner_id=owner_id,
    )
    session.add(db_credential)
    session.commit()
    session.refresh(db_credential)
    return db_credential


def get_credential_with_data(*, session: Session, credential: Credential) -> dict:
    """Get credential and decrypt its data."""
    decrypted_json = decrypt_field(credential.encrypted_data)
    credential_data = json.loads(decrypted_json)
    return credential_data


def update_credential(
    *, session: Session, db_credential: Credential, credential_in: CredentialUpdate
) -> Credential:
    """Update a credential, re-encrypting data if provided."""
    update_dict = credential_in.model_dump(exclude_unset=True)

    # Handle credential_data separately for encryption
    if "credential_data" in update_dict:
        credential_data_json = json.dumps(update_dict["credential_data"])
        encrypted_data = encrypt_field(credential_data_json)
        update_dict.pop("credential_data")
        db_credential.encrypted_data = encrypted_data

    # Update other fields
    db_credential.sqlmodel_update(update_dict)
    session.add(db_credential)
    session.commit()
    session.refresh(db_credential)
    return db_credential


def add_credential_to_agent(
    *, session: Session, agent_id: uuid.UUID, credential_id: uuid.UUID
) -> None:
    """Link a credential to an agent."""
    # Check if link already exists
    statement = select(AgentCredentialLink).where(
        AgentCredentialLink.agent_id == agent_id,
        AgentCredentialLink.credential_id == credential_id,
    )
    existing_link = session.exec(statement).first()
    if existing_link:
        return  # Link already exists, nothing to do

    # Create new link
    link = AgentCredentialLink(agent_id=agent_id, credential_id=credential_id)
    session.add(link)
    session.commit()


def remove_credential_from_agent(
    *, session: Session, agent_id: uuid.UUID, credential_id: uuid.UUID
) -> None:
    """Unlink a credential from an agent."""
    statement = select(AgentCredentialLink).where(
        AgentCredentialLink.agent_id == agent_id,
        AgentCredentialLink.credential_id == credential_id,
    )
    link = session.exec(statement).first()
    if link:
        session.delete(link)
        session.commit()


def get_agent_credentials(*, session: Session, agent_id: uuid.UUID) -> list[Credential]:
    """Get all credentials linked to an agent."""
    statement = (
        select(Credential)
        .join(AgentCredentialLink)
        .where(AgentCredentialLink.agent_id == agent_id)
    )
    credentials = session.exec(statement).all()
    return list(credentials)
