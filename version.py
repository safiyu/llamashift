"""Single source of truth for the LlamaShift release and management-API versions."""

__version__ = "1.0.0"

# Bump on any breaking change to the /api/* contract that clients such as kontexta-cli rely on.
# Additive changes (new endpoints, new response fields) do not bump it.
API_VERSION = 1
