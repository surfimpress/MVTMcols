"""MVTM transcription pipeline.

A two-pass pipeline that turns the column / ad PNGs produced by the
parent project into diplomatic transcripts (pass 1) and then
interpreted *items* with extracted metadata (pass 2). The pipeline
runs at its own pace, lives in its own SQLite database, and reads
parent state read-only via ATTACH DATABASE.

Read ``transcribe/README.md`` for the orchestration loop.
"""
