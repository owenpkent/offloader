Unicode true
RequestExecutionLevel admin
SetCompressor /SOLID lzma
SetDatablockOptimize on

!include "MUI2.nsh"
!include "FileFunc.nsh"
!include "LogicLib.nsh"
!include "nsDialogs.nsh"
!include "WinMessages.nsh"
!include "x64.nsh"

!define PRODUCT_NAME "Offloader"
!define PRODUCT_VERSION "@PRODUCT_VERSION@"
!define BUNDLE_DIR "@BUNDLE_DIR@"
!define OUTPUT_FILE "@OUTPUT_FILE@"

Name "${PRODUCT_NAME} ${PRODUCT_VERSION}"
OutFile "${OUTPUT_FILE}"
InstallDir "$PROGRAMFILES64\Offloader"
BrandingText "Offloader"

VIProductVersion "@WINDOWS_VERSION@"
VIAddVersionKey /LANG=1033 "ProductName" "Offloader"
VIAddVersionKey /LANG=1033 "ProductVersion" "${PRODUCT_VERSION}"
VIAddVersionKey /LANG=1033 "FileVersion" "${PRODUCT_VERSION}"
VIAddVersionKey /LANG=1033 "FileDescription" "Offloader Setup"
VIAddVersionKey /LANG=1033 "OriginalFilename" "Offloader-Setup-${PRODUCT_VERSION}.exe"
VIAddVersionKey /LANG=1033 "LegalCopyright" "Copyright (c) Owen Kent"

Var DesktopShortcut
Var StartMenuShortcut
Var ShortcutDialog
Var PreviousInstallLocation
Var PreviousDesktopShortcut
Var PreviousStartMenuShortcut

!define MUI_ABORTWARNING
!define MUI_WELCOMEPAGE_TITLE "Welcome to the Offloader Setup Wizard"
!define MUI_LICENSEPAGE_TEXT_TOP "Review the license before installing Offloader."
!define MUI_FINISHPAGE_NOAUTOCLOSE
!define MUI_FINISHPAGE_RUN "$INSTDIR\Offloader.exe"
!define MUI_FINISHPAGE_RUN_TEXT "Launch Offloader"
!define MUI_FINISHPAGE_RUN_FUNCTION LaunchOffloader

!insertmacro MUI_PAGE_WELCOME
!insertmacro MUI_PAGE_LICENSE "${BUNDLE_DIR}\LICENSE"
!insertmacro MUI_PAGE_DIRECTORY
Page custom ShortcutsCreate ShortcutsLeave
!insertmacro MUI_PAGE_INSTFILES
!insertmacro MUI_PAGE_FINISH

!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES
!insertmacro MUI_UNPAGE_FINISH

!insertmacro MUI_LANGUAGE "English"

Function .onInit
    ${IfNot} ${RunningX64}
        MessageBox MB_ICONSTOP "Offloader requires 64-bit Windows."
        Abort
    ${EndIf}
    StrCpy $DesktopShortcut "1"
    StrCpy $StartMenuShortcut "1"
    SetShellVarContext all
    SetRegView 64
    ${GetOptions} $CMDLINE "/D=" $0
    IfErrors 0 install_directory_done
    ReadRegStr $0 HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\Offloader" "InstallLocation"
    IfErrors install_directory_done
    StrCpy $INSTDIR $0
install_directory_done:
    ClearErrors
FunctionEnd

Function ShortcutsCreate
    nsDialogs::Create 1018
    Pop $ShortcutDialog
    ${If} $ShortcutDialog == error
        Abort
    ${EndIf}
    ${NSD_CreateCheckbox} 0 8u 100% 12u "Create a desktop shortcut"
    Pop $0
    ${If} $DesktopShortcut == "1"
        ${NSD_SetState} $0 ${BST_CHECKED}
    ${EndIf}
    ${NSD_OnClick} $0 ShortcutsDesktopChanged
    ${NSD_CreateCheckbox} 0 30u 100% 12u "Create Start Menu shortcuts"
    Pop $1
    ${If} $StartMenuShortcut == "1"
        ${NSD_SetState} $1 ${BST_CHECKED}
    ${EndIf}
    ${NSD_OnClick} $1 ShortcutsStartMenuChanged
    nsDialogs::Show
FunctionEnd

Function ShortcutsDesktopChanged
    Pop $0
    ${NSD_GetState} $0 $1
    ${If} $1 == ${BST_CHECKED}
        StrCpy $DesktopShortcut "1"
    ${Else}
        StrCpy $DesktopShortcut "0"
    ${EndIf}
FunctionEnd

Function ShortcutsStartMenuChanged
    Pop $0
    ${NSD_GetState} $0 $1
    ${If} $1 == ${BST_CHECKED}
        StrCpy $StartMenuShortcut "1"
    ${Else}
        StrCpy $StartMenuShortcut "0"
    ${EndIf}
FunctionEnd

Function ShortcutsLeave
FunctionEnd

Function LaunchOffloader
    ${If} ${Silent}
        Return
    ${EndIf}
    ClearErrors
    ExecWait '"$INSTDIR\offloader-maintenance.exe" launch --target "$INSTDIR"' $0
    ${If} ${Errors}
        MessageBox MB_ICONEXCLAMATION "Offloader was installed but could not be launched from this setup session. Start it from the Start Menu."
        Return
    ${EndIf}
    ${If} $0 != 0
        MessageBox MB_ICONEXCLAMATION "Offloader was installed but could not be launched from this setup session. Start it from the Start Menu."
    ${EndIf}
FunctionEnd

Section "Install Offloader" SEC_MAIN
    InitPluginsDir
    ReadRegStr $PreviousInstallLocation HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\Offloader" "InstallLocation"
    ReadRegStr $PreviousDesktopShortcut HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\Offloader" "DesktopShortcut"
    ReadRegStr $PreviousStartMenuShortcut HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\Offloader" "StartMenuShortcut"
    ClearErrors
    SetOutPath "$PLUGINSDIR\payload"
    File /r "${BUNDLE_DIR}\*"
    WriteUninstaller "$PLUGINSDIR\payload\Uninstall.exe"
    IfErrors install_failed
    ClearErrors
    ExecWait '"$PLUGINSDIR\payload\offloader-maintenance.exe" install --payload "$PLUGINSDIR\payload" --target "$INSTDIR"' $0
    IfErrors install_failed
    ${If} $0 != 0
        Goto install_failed
    ${EndIf}
    SetRegView 64
    WriteRegStr HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\Offloader" "DisplayName" "Offloader"
    WriteRegStr HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\Offloader" "DisplayVersion" "${PRODUCT_VERSION}"
    WriteRegStr HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\Offloader" "Publisher" "Offloader contributors"
    WriteRegStr HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\Offloader" "InstallLocation" "$INSTDIR"
    WriteRegStr HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\Offloader" "DisplayIcon" "$INSTDIR\Offloader.exe"
    WriteRegStr HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\Offloader" "UninstallString" "$\"$INSTDIR\Uninstall.exe$\""
    WriteRegDWORD HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\Offloader" "NoModify" 1
    WriteRegDWORD HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\Offloader" "NoRepair" 1
    IfErrors install_failed
    ${If} $DesktopShortcut == "1"
        CreateShortCut "$DESKTOP\Offloader.lnk" "$INSTDIR\Offloader.exe"
    ${ElseIf} $PreviousInstallLocation == $INSTDIR
        ${If} $PreviousDesktopShortcut == "1"
            ${If} ${FileExists} "$DESKTOP\Offloader.lnk"
                Delete "$DESKTOP\Offloader.lnk"
                IfErrors install_failed
            ${EndIf}
        ${EndIf}
    ${EndIf}
    ${If} $StartMenuShortcut == "1"
        CreateDirectory "$SMPROGRAMS\Offloader"
        CreateShortCut "$SMPROGRAMS\Offloader\Offloader.lnk" "$INSTDIR\Offloader.exe"
        CreateShortCut "$SMPROGRAMS\Offloader\Uninstall Offloader.lnk" "$INSTDIR\Uninstall.exe"
    ${ElseIf} $PreviousInstallLocation == $INSTDIR
        ${If} $PreviousStartMenuShortcut == "1"
            ${If} ${FileExists} "$SMPROGRAMS\Offloader\Offloader.lnk"
                Delete "$SMPROGRAMS\Offloader\Offloader.lnk"
                IfErrors install_failed
            ${EndIf}
            ${If} ${FileExists} "$SMPROGRAMS\Offloader\Uninstall Offloader.lnk"
                Delete "$SMPROGRAMS\Offloader\Uninstall Offloader.lnk"
                IfErrors install_failed
            ${EndIf}
            RMDir "$SMPROGRAMS\Offloader"
            ; Keep a directory containing unrelated shortcuts or user files.
            ClearErrors
        ${EndIf}
    ${EndIf}
    WriteRegStr HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\Offloader" "DesktopShortcut" "$DesktopShortcut"
    WriteRegStr HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\Offloader" "StartMenuShortcut" "$StartMenuShortcut"
    IfErrors install_failed
    Goto install_done
install_failed:
    SetErrorLevel 1
    ${IfNot} ${Silent}
        MessageBox MB_ICONSTOP "Offloader could not safely complete this installation."
    ${EndIf}
    Abort
install_done:
SectionEnd

Section "Uninstall"
    SetShellVarContext all
    SetRegView 64
    InitPluginsDir
    ReadRegStr $PreviousInstallLocation HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\Offloader" "InstallLocation"
    ClearErrors
    CopyFiles /SILENT "$INSTDIR\offloader-maintenance.exe" "$PLUGINSDIR\offloader-maintenance.exe"
    IfErrors uninstall_failed
    ClearErrors
    ExecWait '"$PLUGINSDIR\offloader-maintenance.exe" uninstall --target "$INSTDIR"' $0
    IfErrors uninstall_failed
    ${If} $0 != 0
        Goto uninstall_failed
    ${EndIf}
    StrCmp $PreviousInstallLocation $INSTDIR 0 uninstall_cleanup_done
    IfFileExists "$DESKTOP\Offloader.lnk" 0 +3
        ClearErrors
        Delete "$DESKTOP\Offloader.lnk"
        IfErrors uninstall_failed
    IfFileExists "$SMPROGRAMS\Offloader\Offloader.lnk" 0 +3
        ClearErrors
        Delete "$SMPROGRAMS\Offloader\Offloader.lnk"
        IfErrors uninstall_failed
    IfFileExists "$SMPROGRAMS\Offloader\Uninstall Offloader.lnk" 0 +3
        ClearErrors
        Delete "$SMPROGRAMS\Offloader\Uninstall Offloader.lnk"
        IfErrors uninstall_failed
    ClearErrors
    RMDir "$SMPROGRAMS\Offloader"
    ClearErrors
    DeleteRegKey HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\Offloader"
    IfErrors uninstall_failed
uninstall_cleanup_done:
    ClearErrors
    RMDir "$INSTDIR"
    Goto uninstall_done
uninstall_failed:
    SetErrorLevel 1
    ${IfNot} ${Silent}
        MessageBox MB_ICONSTOP "Offloader could not safely uninstall because it is in use or its files have changed."
    ${EndIf}
    Abort
uninstall_done:
SectionEnd

@UNINSTALL_FINALIZE@
