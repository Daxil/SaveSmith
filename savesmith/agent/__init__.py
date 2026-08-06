"""Discovery: working out an unknown save format.

The expensive half of SaveSmith, and the half that should run as rarely as
possible. Everything cheap lives in ``savesmith.core`` and is tried first — the
decoder ladder, the checksum search, the two-save diff. Only a file that
survives all of that reaches a model.
"""
