"""Read-only web view of runs.db.

`observer` opens runs.db read-only and reads each projection inside one read
transaction; `projection` turns that snapshot into the Office view model;
`identity` names hosts, repositories, runs, tasks, dispatches and sessions
stably; `activity` pages redacted events; `synthetic` builds test workspaces.
Nothing in this package writes to runs.db.
"""
