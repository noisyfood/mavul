# Assume a Trusted Runtime

MAVUL initially assumes that its users, configuration, host Agent code, and
configured MCP services are trusted. It will not add authentication,
multi-tenant isolation, or internal security capabilities; device leases and
operation records remain because they coordinate work and make failures
traceable.
