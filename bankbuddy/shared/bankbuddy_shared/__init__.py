# BankBuddy shared - interfaces and contracts
#
# This package contains ONLY abstractions:
#   - `interfaces/`  Abstract Base Classes (ABCs) that every concrete
#                    provider must implement.
#   - `contracts/`   Pydantic DTOs exchanged across service boundaries.
#
# No vendor SDKs are imported here. This is the hinge of the
# Dependency Inversion Principle for the whole codebase.
__version__ = "0.1.0"
