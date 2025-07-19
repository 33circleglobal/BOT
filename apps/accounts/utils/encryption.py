from cryptography.fernet import Fernet

from config.settings import FERNET_SECRET_KEY

fernet = Fernet(FERNET_SECRET_KEY)


def encrypt_value(value: str) -> str:
    return fernet.encrypt(value.encode()).decode()


def decrypt_value(value: str) -> str:
    return fernet.decrypt(value.encode()).decode()
