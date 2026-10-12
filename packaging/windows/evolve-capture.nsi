; Evolve Capture installer for Windows (NSIS 3, Modern UI 2).
;
; Per-user, no admin rights: installs to %LOCALAPPDATA%\Programs\Evolve Capture.
; Wraps the PyInstaller onedir build (packaging/dist/EvolveCapture). Unsigned.
; Build it with packaging/windows/build.ps1, which passes:
;   /DVERSION=0.2.4  /DVERSION4=0.2.4.0  /DSRCDIR=<dist\EvolveCapture>  /DOUTFILE=<setup.exe>
;
; Plan: docs/plans/packaging.md ("3. Windows"). Notes: docs/plans/packaging-spikes.md.
;
; Start at login: HKCU\...\Run, value "Evolve Capture" = "<INSTDIR>\EvolveCapture.exe"
; (quoted, no arguments). evolve_capture/startup.py writes the same value in the
; frozen app; keep the two in step.

Unicode true
ManifestDPIAware true
RequestExecutionLevel user
SetCompressor /SOLID lzma
SetCompressorDictSize 64

!ifndef VERSION
  !error "Pass the version: makensis /DVERSION=x.y.z (packaging/windows/build.ps1 does it)"
!endif
!ifndef VERSION4
  !define VERSION4 "0.0.0.0"
!endif
!ifndef SRCDIR
  !define SRCDIR "..\dist\EvolveCapture"
!endif
!ifndef OUTFILE
  !define OUTFILE "..\dist\EvolveCapture-${VERSION}-win-x64-setup.exe"
!endif

!define APPNAME   "Evolve Capture"
!define EXENAME   "EvolveCapture.exe"
!define APPKEY    "Software\Evolve Capture"
!define RUNKEY    "Software\Microsoft\Windows\CurrentVersion\Run"
!define RUNVALUE  "Evolve Capture"
!define UNINSTKEY "Software\Microsoft\Windows\CurrentVersion\Uninstall\Evolve Capture"
; Before 0.3 the app was called ScreenMind. RemoveOldScreenMind uninstalls it.
!define OLDAPPKEY    "Software\ScreenMind"
!define OLDRUNVALUE  "ScreenMind"
!define OLDUNINSTKEY "Software\Microsoft\Windows\CurrentVersion\Uninstall\ScreenMind"

!include "MUI2.nsh"
!include "nsDialogs.nsh"
!include "LogicLib.nsh"
!include "x64.nsh"
!include "FileFunc.nsh"

Name "${APPNAME}"
OutFile "${OUTFILE}"
InstallDir "$LOCALAPPDATA\Programs\${APPNAME}"
InstallDirRegKey HKCU "${APPKEY}" "InstallDir"
BrandingText "${APPNAME} ${VERSION}"
ShowInstDetails show
ShowUninstDetails show

Var UnderstandBox
Var Understood      ; 1 once "I understand" is ticked
Var StartBox
Var StartAtLogin    ; 1 = write the Run value
Var DataDir
Var TotalMB
Var ModelsMB
Var SizeText

; ── Pages ──────────────────────────────────────────────────────────────

!define MUI_ICON   "..\..\evolve_capture\assets\favicon.ico"
!define MUI_UNICON "..\..\evolve_capture\assets\favicon.ico"
!define MUI_ABORTWARNING

!define MUI_WELCOMEPAGE_TITLE "Install Evolve Capture"
!define MUI_WELCOMEPAGE_TEXT "This installs Evolve Capture ${VERSION} for your Windows account only. It needs no administrator rights.$\r$\n$\r$\nEvolve Capture runs in the background and keeps a private journal of what is on your screen. The next page explains what it records.$\r$\n$\r$\nClick Next to continue."
!define MUI_PAGE_CUSTOMFUNCTION_SHOW EnableNext
!insertmacro MUI_PAGE_WELCOME

Page custom RecordsPageCreate RecordsPageLeave

!define MUI_PAGE_CUSTOMFUNCTION_SHOW EnableNext
!insertmacro MUI_PAGE_DIRECTORY

Page custom OptionsPageCreate OptionsPageLeave

!insertmacro MUI_PAGE_INSTFILES

!define MUI_FINISHPAGE_TITLE "Evolve Capture is installed"
!define MUI_FINISHPAGE_TEXT "You find Evolve Capture in the Start menu. To remove it, use Settings > Apps > Installed apps."
!define MUI_FINISHPAGE_RUN
!define MUI_FINISHPAGE_RUN_TEXT "Start Evolve Capture now"
!define MUI_FINISHPAGE_RUN_FUNCTION StartApp
!insertmacro MUI_PAGE_FINISH

!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES

!insertmacro MUI_LANGUAGE "English"

VIProductVersion "${VERSION4}"
VIAddVersionKey /LANG=${LANG_ENGLISH} "ProductName" "${APPNAME}"
VIAddVersionKey /LANG=${LANG_ENGLISH} "ProductVersion" "${VERSION}"
VIAddVersionKey /LANG=${LANG_ENGLISH} "FileVersion" "${VERSION}"
VIAddVersionKey /LANG=${LANG_ENGLISH} "FileDescription" "${APPNAME} Setup"
VIAddVersionKey /LANG=${LANG_ENGLISH} "LegalCopyright" "MIT License"

; ── Shared helpers (installer and uninstaller) ────────────────────────

; Stops the EvolveCapture.exe that runs from $INSTDIR: asks it to stop cleanly,
; waits, then kills it. See stop-evolve-capture.ps1. Never touches other copies.
!macro STOP_APP_FUNC un
Function ${un}StopRunningApp
  InitPluginsDir
  File "/oname=$PLUGINSDIR\stop-evolve-capture.ps1" "stop-evolve-capture.ps1"
  retry:
  DetailPrint "Stopping Evolve Capture if it is running..."
  ; 64-bit PowerShell, so it sees the full path of the 64-bit app.
  ${DisableX64FSRedirection}
  nsExec::ExecToLog '"$SYSDIR\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "$PLUGINSDIR\stop-evolve-capture.ps1" -ExePath "$INSTDIR\${EXENAME}"'
  Pop $0
  ${EnableX64FSRedirection}
  ${If} $0 != 0
    MessageBox MB_RETRYCANCEL|MB_ICONEXCLAMATION "Evolve Capture is still running and could not be stopped.$\r$\n$\r$\nEnd EvolveCapture.exe in Task Manager, then click Retry." /SD IDCANCEL IDRETRY retry
    Abort "Evolve Capture is still running."
  ${EndIf}
FunctionEnd
!macroend
!insertmacro STOP_APP_FUNC ""
!insertmacro STOP_APP_FUNC "un."

; Removes the Run value only when it starts this copy. A value that starts
; something else (for example a dev checkout) stays.
!macro REMOVE_RUN_FUNC un
Function ${un}RemoveRunValueIfOurs
  ReadRegStr $0 HKCU "${RUNKEY}" "${RUNVALUE}"
  ${If} $0 == '"$INSTDIR\${EXENAME}"'
  ${OrIf} $0 == "$INSTDIR\${EXENAME}"
    DeleteRegValue HKCU "${RUNKEY}" "${RUNVALUE}"
  ${EndIf}
FunctionEnd
!macroend
!insertmacro REMOVE_RUN_FUNC ""
!insertmacro REMOVE_RUN_FUNC "un."

; ── Installer ─────────────────────────────────────────────────────────

; Uninstalls ScreenMind, the old name of this app, silently: its own
; uninstaller stops it and removes its files, Start menu link and Run value.
; A silent uninstall keeps the data in $PROFILE\.screenmind; the app moves it
; to $PROFILE\.evolve-capture on its first start.
Function RemoveOldScreenMind
  ReadRegStr $0 HKCU "${OLDUNINSTKEY}" "InstallLocation"
  ${If} $0 == ""
  ${OrIfNot} ${FileExists} "$0\uninstall.exe"
    Return
  ${EndIf}
  DetailPrint "Removing ScreenMind (the old name of this app) from $0..."
  ; _?= runs the uninstaller in place, so ExecWait waits for it
  ExecWait '"$0\uninstall.exe" /S _?=$0' $1
  ${If} $1 != 0
    DetailPrint "The ScreenMind uninstaller returned $1. Remove it in Settings > Apps if it is still there."
    Return
  ${EndIf}
  Delete "$0\uninstall.exe"
  RMDir "$0"
FunctionEnd

Function .onInit
  SetShellVarContext current
  ${IfNot} ${RunningX64}
    MessageBox MB_ICONSTOP "Evolve Capture needs 64-bit Windows." /SD IDOK
    Abort
  ${EndIf}

  ; One setup at a time.
  System::Call 'kernel32::CreateMutexW(p 0, i 0, w "EvolveCaptureSetup") p .r1 ?e'
  Pop $0
  ${If} $0 == 183 ; ERROR_ALREADY_EXISTS
    MessageBox MB_ICONEXCLAMATION "Evolve Capture Setup is already running." /SD IDOK
    Abort
  ${EndIf}

  ; Start at login: on by default. An upgrade keeps the earlier choice,
  ; also one made under the old name (ScreenMind).
  StrCpy $StartAtLogin 1
  ReadRegStr $0 HKCU "${APPKEY}" "InstallDir"
  ${If} $0 != ""
    ReadRegStr $1 HKCU "${RUNKEY}" "${RUNVALUE}"
    ${If} $1 == ""
      StrCpy $StartAtLogin 0
    ${EndIf}
  ${Else}
    ReadRegStr $0 HKCU "${OLDAPPKEY}" "InstallDir"
    ${If} $0 != ""
      ReadRegStr $1 HKCU "${RUNKEY}" "${OLDRUNVALUE}"
      ${If} $1 == ""
        StrCpy $StartAtLogin 0
      ${EndIf}
    ${EndIf}
  ${EndIf}
  ; Silent installs: /NOSTARTUP leaves start at login off.
  ${GetParameters} $0
  ClearErrors
  ${GetOptions} $0 "/NOSTARTUP" $1
  ${IfNot} ${Errors}
    StrCpy $StartAtLogin 0
  ${EndIf}
FunctionEnd

Function EnableNext
  GetDlgItem $0 $HWNDPARENT 1
  EnableWindow $0 1
FunctionEnd

Function RecordsPageCreate
  !insertmacro MUI_HEADER_TEXT "What Evolve Capture records" "Please read this before you install."
  nsDialogs::Create 1018
  Pop $0
  ${If} $0 == error
    Abort
  ${EndIf}

  ${NSD_CreateLabel} 0 0 100% 112u "While it runs, Evolve Capture records:$\r$\n$\r$\n\
• A screenshot of your screen about every 10 seconds, and right after you click, switch apps or type.$\r$\n\
• The name and title of the active window, and the text on screen (read from the app, or recognized in the screenshot).$\r$\n\
• Your clicks, app switches, the text you type and what you copy to the clipboard. Password fields are skipped. This is on by default and can be turned off.$\r$\n\
• The sound of your calls, only if you turn on call transcription. It is off by default.$\r$\n$\r$\n\
Everything stays on this computer, in $PROFILE\.evolve-capture. Nothing is uploaded. When you uninstall, you can delete it all."
  Pop $0

  ${NSD_CreateCheckbox} 0 120u 100% 12u "I understand what Evolve Capture records"
  Pop $UnderstandBox
  ${NSD_OnClick} $UnderstandBox RecordsToggle
  ${If} $Understood == 1
    ${NSD_Check} $UnderstandBox
  ${EndIf}
  Call RecordsUpdateNext
  nsDialogs::Show
FunctionEnd

Function RecordsToggle
  Pop $0
  ${NSD_GetState} $UnderstandBox $Understood
  Call RecordsUpdateNext
FunctionEnd

Function RecordsUpdateNext
  GetDlgItem $0 $HWNDPARENT 1
  ${If} $Understood == 1
    EnableWindow $0 1
  ${Else}
    EnableWindow $0 0
  ${EndIf}
FunctionEnd

Function RecordsPageLeave
  ${If} $Understood != 1
    Abort
  ${EndIf}
FunctionEnd

Function OptionsPageCreate
  !insertmacro MUI_HEADER_TEXT "Start at sign-in" "Choose when Evolve Capture runs."
  Call EnableNext
  nsDialogs::Create 1018
  Pop $0
  ${If} $0 == error
    Abort
  ${EndIf}

  ${NSD_CreateCheckbox} 0 0 100% 12u "Start Evolve Capture when I sign in"
  Pop $StartBox
  ${If} $StartAtLogin == 1
    ${NSD_Check} $StartBox
  ${EndIf}
  ${NSD_CreateLabel} 12u 16u -12u 36u "Recommended. Evolve Capture then records from the moment you sign in to Windows, without a window."
  Pop $0
  nsDialogs::Show
FunctionEnd

Function OptionsPageLeave
  ${NSD_GetState} $StartBox $StartAtLogin
FunctionEnd

Function StartApp
  SetOutPath "$INSTDIR"
  Exec '"$INSTDIR\${EXENAME}"'
FunctionEnd

Section "Evolve Capture" SecMain
  SectionIn RO
  SetShellVarContext current

  Call RemoveOldScreenMind
  Call StopRunningApp

  ; An upgrade must not keep modules from the older build. llama\ is where a
  ; bundled llama-server would go (F3); the app never installs into it.
  ${If} ${FileExists} "$INSTDIR\${EXENAME}"
    DetailPrint "Removing the older version..."
    RMDir /r "$INSTDIR\_internal"
    RMDir /r "$INSTDIR\llama"
  ${EndIf}

  SetOutPath "$INSTDIR"
  File /r "${SRCDIR}\*.*"
  WriteUninstaller "$INSTDIR\uninstall.exe"

  CreateShortcut "$SMPROGRAMS\${APPNAME}.lnk" "$INSTDIR\${EXENAME}" "" "$INSTDIR\${EXENAME}" 0

  WriteRegStr HKCU "${APPKEY}" "InstallDir" "$INSTDIR"
  ${If} $StartAtLogin == 1
    WriteRegStr HKCU "${RUNKEY}" "${RUNVALUE}" '"$INSTDIR\${EXENAME}"'
  ${Else}
    Call RemoveRunValueIfOurs
  ${EndIf}

  WriteRegStr HKCU "${UNINSTKEY}" "DisplayName" "${APPNAME}"
  WriteRegStr HKCU "${UNINSTKEY}" "DisplayVersion" "${VERSION}"
  WriteRegStr HKCU "${UNINSTKEY}" "Publisher" "${APPNAME}"
  WriteRegStr HKCU "${UNINSTKEY}" "DisplayIcon" "$INSTDIR\${EXENAME}"
  WriteRegStr HKCU "${UNINSTKEY}" "InstallLocation" "$INSTDIR"
  WriteRegStr HKCU "${UNINSTKEY}" "UninstallString" '"$INSTDIR\uninstall.exe"'
  WriteRegStr HKCU "${UNINSTKEY}" "QuietUninstallString" '"$INSTDIR\uninstall.exe" /S'
  WriteRegDWORD HKCU "${UNINSTKEY}" "NoModify" 1
  WriteRegDWORD HKCU "${UNINSTKEY}" "NoRepair" 1
  ${GetSize} "$INSTDIR" "/S=0K" $0 $1 $2
  WriteRegDWORD HKCU "${UNINSTKEY}" "EstimatedSize" $0
SectionEnd

; ── Uninstaller ───────────────────────────────────────────────────────

Function un.onInit
  SetShellVarContext current
FunctionEnd

; $0 = size in MB -> $SizeText ("350 MB" or "2.3 GB")
Function un.FormatMB
  ${If} $0 < 1024
    StrCpy $SizeText "$0 MB"
  ${Else}
    IntOp $1 $0 / 1024
    IntOp $2 $0 * 10
    IntOp $2 $2 / 1024
    IntOp $2 $2 % 10
    StrCpy $SizeText "$1.$2 GB"
  ${EndIf}
FunctionEnd

; Deletes everything in $DataDir except the models and llama folders.
Function un.DeleteDataKeepModels
  FindFirst $0 $1 "$DataDir\*"
  ${DoWhile} $1 != ""
    ${If} $1 != "."
    ${AndIf} $1 != ".."
    ${AndIf} $1 != "models"
    ${AndIf} $1 != "llama"
      ${If} ${FileExists} "$DataDir\$1\*.*"
        RMDir /r "$DataDir\$1"
      ${Else}
        Delete "$DataDir\$1"
      ${EndIf}
    ${EndIf}
    FindNext $0 $1
  ${Loop}
  FindClose $0
FunctionEnd

; Asks twice, default No: the data (screenshots, database, settings), then the
; models (large downloads, needed again on a reinstall). Silent: keeps both.
Function un.AskDeleteData
  StrCpy $DataDir "$PROFILE\.evolve-capture"
  ${IfNot} ${FileExists} "$DataDir\*.*"
  ${OrIf} ${Silent}
    Return
  ${EndIf}

  DetailPrint "Measuring your Evolve Capture data..."
  ${GetSize} "$DataDir" "/S=0M" $TotalMB $1 $2
  StrCpy $ModelsMB 0
  ${If} ${FileExists} "$DataDir\models\*.*"
    ${GetSize} "$DataDir\models" "/S=0M" $0 $1 $2
    IntOp $ModelsMB $ModelsMB + $0
  ${EndIf}
  ${If} ${FileExists} "$DataDir\llama\*.*"
    ${GetSize} "$DataDir\llama" "/S=0M" $0 $1 $2
    IntOp $ModelsMB $ModelsMB + $0
  ${EndIf}

  IntOp $0 $TotalMB - $ModelsMB
  Call un.FormatMB
  StrCpy $3 0
  MessageBox MB_YESNO|MB_ICONQUESTION|MB_DEFBUTTON2 "Also delete your Evolve Capture data (screenshots, database, settings: $SizeText)?$\r$\n$\r$\nIt is in $DataDir. Choose No to keep it." IDNO +2
    StrCpy $3 1

  StrCpy $4 0
  ${If} ${FileExists} "$DataDir\models\*.*"
  ${OrIf} ${FileExists} "$DataDir\llama\*.*"
    StrCpy $0 $ModelsMB
    Call un.FormatMB
    MessageBox MB_YESNO|MB_ICONQUESTION|MB_DEFBUTTON2 "Also delete the downloaded models ($SizeText)?$\r$\n$\r$\nEvolve Capture downloads them again if you reinstall it. Choose No to keep them." IDNO +2
      StrCpy $4 1
  ${EndIf}

  ${If} $3 == 1
  ${AndIf} $4 == 1
    DetailPrint "Deleting $DataDir"
    RMDir /r "$DataDir"
  ${ElseIf} $3 == 1
    DetailPrint "Deleting your data, keeping the models"
    Call un.DeleteDataKeepModels
  ${ElseIf} $4 == 1
    DetailPrint "Deleting the models"
    RMDir /r "$DataDir\models"
    RMDir /r "$DataDir\llama"
  ${EndIf}
FunctionEnd

Section "Uninstall"
  SetShellVarContext current

  Call un.StopRunningApp

  Delete "$SMPROGRAMS\${APPNAME}.lnk"
  Call un.RemoveRunValueIfOurs

  ; Only our own files: $INSTDIR may be a folder the user picked.
  RMDir /r "$INSTDIR\_internal"
  RMDir /r "$INSTDIR\llama"
  Delete "$INSTDIR\${EXENAME}"
  Delete "$INSTDIR\uninstall.exe"
  RMDir "$INSTDIR"

  DeleteRegKey HKCU "${UNINSTKEY}"
  DeleteRegKey HKCU "${APPKEY}"

  Call un.AskDeleteData
SectionEnd
