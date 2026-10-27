"""Process exit codes shared by the long-running services."""

EXIT_OK = 0
EXIT_STARTUP_FAILED = 1
EXIT_LEASE_LOST = 3  # lease lost or database failure: the supervisor restarts the process
EXIT_CONFIG_RELOAD = 4  # an approved agent-config change was applied: the supervisor restarts
