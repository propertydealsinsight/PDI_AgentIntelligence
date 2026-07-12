@echo off
REM ============================================================================
REM Weekly agent-performance refresh (Task Scheduler entry point on Windows;
REM on Linux use the equivalent single crontab line).
REM
REM ONE step does everything, in order, inside the job itself:
REM   build staging (~4.5-5h, live table untouched and serving throughout)
REM   -> structural pre-swap gate (indexes, row counts, duplicates, invariants)
REM   -> pre-swap RAG validation of the STAGING table vs live (random sample)
REM   -> only if both pass: atomic RENAME swap (milliseconds, zero outage)
REM Any failure = swap blocked, live table untouched, email explains why.
REM ============================================================================
setlocal
cd /d C:\PDI\GitHub\PDI_AgentIntelligence\PDI_Agent_Performance

set PY=env\Scripts\python.exe
if not exist %PY% set PY=python

echo [%date% %time%] weekly run starting >> logs\weekly_run.log
%PY% process_agent_performance.py >> logs\weekly_run.log 2>&1
echo [%date% %time%] weekly run finished, exit code %errorlevel% >> logs\weekly_run.log

endlocal
