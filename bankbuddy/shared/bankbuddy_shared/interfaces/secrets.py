"""Secret-store interface.

Implementations:
    - EnvSecretProvider           (default, reads os.environ)
    - AzureKeyVaultProvider       (future)
    - AWSSecretsManagerProvider   (future)
    - VaultProvider               (future, HashiCorp Vault)
"""
from __future__ import annotations

from abc import ABC, abstractmethod


class ISecretProvider(ABC):
    @abstractmethod
    async def get_secret(self, name: str) -> str:
        """Return the secret value for `name`. Raises KeyError if missing."""
