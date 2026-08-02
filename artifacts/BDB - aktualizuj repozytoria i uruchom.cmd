@echo off
setlocal EnableExtensions EnableDelayedExpansion
chcp 65001 >nul
title BDB - aktualizacja repozytoriow i uruchomienie sesji

set "BDB_REPO=C:\Projekty\DevMaster\bartosz-dev-bridge"
set "BDB_REMOTE=origin"
set "BDB_REMOTE_URL=https://github.com/eagleblastmusic-lgtm/bartosz-dev-bridge.git"
set "BDB_LOCAL_BRANCH=main"
set "BDB_REMOTE_BRANCH=main"

set "GICLEE_REPO=C:\Projekty\GicleeArt"
set "GICLEE_REMOTE=bdbmirror"
set "GICLEE_REMOTE_URL=https://github.com/eagleblastmusic-lgtm/gicleeart-bdb.git"
set "GICLEE_LOCAL_BRANCH=master"
set "GICLEE_REMOTE_BRANCH=main"

set "SESSION_SCRIPT=%BDB_REPO%\scripts\Invoke-GicleeAppBDBSession.ps1"
set "FAILURES=0"
set "CHECK_ONLY=0"
if /I "%~1"=="--check" set "CHECK_ONLY=1"

echo ============================================================
echo  BDB - commit, GitHub i uruchomienie/odnowienie sesji
echo ============================================================
echo.

call :CHECK_TOOLS
call :CHECK_REPO "Bartosz Dev Bridge" "%BDB_REPO%" "%BDB_REMOTE%" "%BDB_REMOTE_URL%" "%BDB_LOCAL_BRANCH%"
call :CHECK_REPO "GicleeArt BDB" "%GICLEE_REPO%" "%GICLEE_REMOTE%" "%GICLEE_REMOTE_URL%" "%GICLEE_LOCAL_BRANCH%"

if not exist "%SESSION_SCRIPT%" (
  echo [BLAD] Brak skryptu sesji: "%SESSION_SCRIPT%"
  set /a FAILURES+=1
)

if not "!FAILURES!"=="0" goto :PREFLIGHT_FAILED

if "!CHECK_ONLY!"=="1" (
  echo [OK] Kontrola konfiguracji zakonczona pomyslnie.
  exit /b 0
)

echo [1/4] Zatrzymywanie poprzedniej sesji BDB...
pwsh -NoProfile -ExecutionPolicy Bypass -File "%SESSION_SCRIPT%" -Action Stop
if errorlevel 1 (
  echo [OSTRZEZENIE] Nie udalo sie poprawnie zatrzymac poprzedniej sesji.
  set /a FAILURES+=1
) else (
  echo [OK] Poprzednia sesja zostala zatrzymana.
)
echo.

echo [2/4] Commit i synchronizacja Bartosz Dev Bridge...
call :COMMIT_AND_PUSH "Bartosz Dev Bridge" "%BDB_REPO%" "%BDB_REMOTE%" "%BDB_LOCAL_BRANCH%" "%BDB_REMOTE_BRANCH%" "all"
echo.

echo [3/4] Commit i synchronizacja prywatnego mirrora GicleeArt...
call :COMMIT_AND_PUSH "GicleeArt BDB" "%GICLEE_REPO%" "%GICLEE_REMOTE%" "%GICLEE_LOCAL_BRANCH%" "%GICLEE_REMOTE_BRANCH%" "giclee"
echo.

echo [4/4] Uruchamianie i odnawianie uzbrojenia BDB...
pwsh -NoProfile -ExecutionPolicy Bypass -File "%SESSION_SCRIPT%" -Action Start
if errorlevel 1 (
  echo [BLAD] Nie udalo sie uruchomic lub uzbroic BDB.
  set /a FAILURES+=1
) else (
  echo [OK] BDB dziala, a czas uzbrojenia zostal odnowiony.
)

echo.
echo ============================================================
if "!FAILURES!"=="0" (
  echo  GOTOWE - oba repozytoria sa zsynchronizowane, BDB dziala.
) else (
  echo  ZAKONCZONO Z OSTRZEZENIAMI/BLADAMI: !FAILURES!
  echo  Przewin wyzej, aby zobaczyc szczegoly. BDB moglo zostac
  echo  uruchomione mimo bledu synchronizacji GitHub.
)
echo ============================================================
echo.
pause
exit /b !FAILURES!

:PREFLIGHT_FAILED
echo.
echo [BLAD] Kontrola wstepna nie powiodla sie. Niczego nie commitowano.
if "!CHECK_ONLY!"=="0" pause
exit /b !FAILURES!

:CHECK_TOOLS
where git >nul 2>&1
if errorlevel 1 (
  echo [BLAD] Nie znaleziono programu git w PATH.
  set /a FAILURES+=1
)
where pwsh >nul 2>&1
if errorlevel 1 (
  echo [BLAD] Nie znaleziono PowerShell 7 ^(pwsh^) w PATH.
  set /a FAILURES+=1
)
goto :eof

:CHECK_REPO
set "CHECK_NAME=%~1"
set "CHECK_PATH=%~2"
set "CHECK_REMOTE=%~3"
set "CHECK_EXPECTED_URL=%~4"
set "CHECK_EXPECTED_BRANCH=%~5"

if not exist "%CHECK_PATH%\.git" (
  echo [BLAD] %CHECK_NAME%: brak repozytorium "%CHECK_PATH%".
  set /a FAILURES+=1
  goto :eof
)

set "CHECK_ACTUAL_URL="
for /f "usebackq delims=" %%R in (`git -C "%CHECK_PATH%" remote get-url "%CHECK_REMOTE%" 2^>nul`) do set "CHECK_ACTUAL_URL=%%R"
if /I not "!CHECK_ACTUAL_URL!"=="%CHECK_EXPECTED_URL%" (
  echo [BLAD] %CHECK_NAME%: remote "%CHECK_REMOTE%" ma nieoczekiwany adres.
  echo        Oczekiwano: %CHECK_EXPECTED_URL%
  echo        Odczytano:  !CHECK_ACTUAL_URL!
  set /a FAILURES+=1
)

set "CHECK_ACTUAL_BRANCH="
for /f "usebackq delims=" %%B in (`git -C "%CHECK_PATH%" branch --show-current 2^>nul`) do set "CHECK_ACTUAL_BRANCH=%%B"
if /I not "!CHECK_ACTUAL_BRANCH!"=="%CHECK_EXPECTED_BRANCH%" (
  echo [BLAD] %CHECK_NAME%: aktywna galaz to "!CHECK_ACTUAL_BRANCH!", oczekiwano "%CHECK_EXPECTED_BRANCH%".
  set /a FAILURES+=1
)
goto :eof

:COMMIT_AND_PUSH
set "SYNC_NAME=%~1"
set "SYNC_PATH=%~2"
set "SYNC_REMOTE=%~3"
set "SYNC_LOCAL_BRANCH=%~4"
set "SYNC_REMOTE_BRANCH=%~5"
set "SYNC_MODE=%~6"

echo [INFO] Dodawanie lokalnych zmian: %SYNC_NAME%
if /I "%SYNC_MODE%"=="giclee" (
  git -C "%SYNC_PATH%" add -A -- . ":(exclude).codex-theme-dev*.log"
) else (
  git -C "%SYNC_PATH%" add -A -- .
)
if errorlevel 1 (
  echo [BLAD] %SYNC_NAME%: git add nie powiodl sie.
  set /a FAILURES+=1
  goto :eof
)

git -C "%SYNC_PATH%" diff --cached --quiet
if errorlevel 1 (
  set "SYNC_STAMP="
  for /f "usebackq delims=" %%T in (`pwsh -NoProfile -Command "Get-Date -Format 'yyyy-MM-dd HH:mm:ss'"`) do set "SYNC_STAMP=%%T"
  git -C "%SYNC_PATH%" commit -m "Automatyczna aktualizacja lokalna: !SYNC_STAMP!"
  if errorlevel 1 (
    echo [BLAD] %SYNC_NAME%: nie udalo sie utworzyc commita.
    set /a FAILURES+=1
    goto :eof
  )
) else (
  echo [INFO] %SYNC_NAME%: brak nowych zmian do commitowania.
)

echo [INFO] Pobieranie informacji o zdalnej galezi...
git -C "%SYNC_PATH%" fetch --prune "%SYNC_REMOTE%" "%SYNC_REMOTE_BRANCH%"
if errorlevel 1 (
  echo [BLAD] %SYNC_NAME%: git fetch nie powiodl sie.
  echo        Lokalny commit pozostaje bezpiecznie zapisany.
  set /a FAILURES+=1
  goto :eof
)

set "SYNC_REMOTE_REF=%SYNC_REMOTE%/%SYNC_REMOTE_BRANCH%"
git -C "%SYNC_PATH%" merge-base --is-ancestor "!SYNC_REMOTE_REF!" "%SYNC_LOCAL_BRANCH%"
if not errorlevel 1 goto :PUSH_REPO

git -C "%SYNC_PATH%" merge-base --is-ancestor "%SYNC_LOCAL_BRANCH%" "!SYNC_REMOTE_REF!"
if errorlevel 1 (
  echo [BLAD] %SYNC_NAME%: lokalna i zdalna historia sa rozgalezione.
  echo        Automatyczne scalanie zostalo celowo zatrzymane.
  echo        Lokalny commit pozostaje bezpiecznie zapisany.
  set /a FAILURES+=1
  goto :eof
)

echo [INFO] Zdalna galaz jest nowsza - wykonywany jest bezpieczny fast-forward.
git -C "%SYNC_PATH%" merge --ff-only "!SYNC_REMOTE_REF!"
if errorlevel 1 (
  echo [BLAD] %SYNC_NAME%: fast-forward nie powiodl sie.
  set /a FAILURES+=1
  goto :eof
)

:PUSH_REPO
echo [INFO] Wysylanie do GitHub: %SYNC_LOCAL_BRANCH% -^> %SYNC_REMOTE%/%SYNC_REMOTE_BRANCH%
git -C "%SYNC_PATH%" push "%SYNC_REMOTE%" "%SYNC_LOCAL_BRANCH%:refs/heads/%SYNC_REMOTE_BRANCH%"
if errorlevel 1 (
  echo [BLAD] %SYNC_NAME%: git push nie powiodl sie.
  echo        Commit pozostaje lokalnie i mozna wyslac go pozniej.
  set /a FAILURES+=1
  goto :eof
)

set "SYNC_LOCAL_SHA="
set "SYNC_REMOTE_SHA="
for /f "usebackq delims=" %%S in (`git -C "%SYNC_PATH%" rev-parse "%SYNC_LOCAL_BRANCH%"`) do set "SYNC_LOCAL_SHA=%%S"
for /f "tokens=1" %%S in ('git -C "%SYNC_PATH%" ls-remote --heads "%SYNC_REMOTE%" "refs/heads/%SYNC_REMOTE_BRANCH%"') do set "SYNC_REMOTE_SHA=%%S"
if /I not "!SYNC_LOCAL_SHA!"=="!SYNC_REMOTE_SHA!" (
  echo [BLAD] %SYNC_NAME%: GitHub nie potwierdzil oczekiwanego commita.
  set /a FAILURES+=1
  goto :eof
)

echo [OK] %SYNC_NAME%: GitHub potwierdzil commit !SYNC_LOCAL_SHA!.
goto :eof
