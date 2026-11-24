; MangaTrans 安装钩子：覆盖前清理残留 sidecar，安装后把关键文件缺失从「静默失败」变成可定位错误。
; 该文件只被 Tauri NSIS 安装器 include，不参与应用运行时。

!macro MT_WRITE_INSTALL_CHECK_MESSAGE
  FileOpen $9 "$TEMP\mangatrans-install-check.log" w
  FileWrite $9 "$INSTDIR$\r$\n"
  FileWrite $9 "安装后自检失败：${MT_INSTALL_CHECK_MESSAGE}$\r$\n"
  FileClose $9
  DetailPrint "${MT_INSTALL_CHECK_MESSAGE}"
!macroend

!macro NSIS_HOOK_PREINSTALL
  ; 覆盖安装前先关闭旧主程序，再清理 sidecar。
  ; 只杀 sidecar 会让旧窗口继续运行并显示“本地服务已退出”，也会干扰新版本启动。
  DetailPrint "关闭正在运行的 MangaTrans"
  nsExec::ExecToLog 'taskkill /F /IM mangatrans.exe'
  Pop $0
  DetailPrint "关闭残留的本地翻译服务"
  nsExec::ExecToLog 'taskkill /F /IM mangatrans-backend.exe'
  Pop $0
!macroend

!macro NSIS_HOOK_POSTINSTALL
  ${IfNot} ${FileExists} "$INSTDIR\mangatrans.exe"
    !define MT_INSTALL_CHECK_MESSAGE "缺少主程序 mangatrans.exe；安装包可能不完整"
    !insertmacro MT_WRITE_INSTALL_CHECK_MESSAGE
    IfSilent mtPostInstallAbort
    MessageBox MB_ICONSTOP|MB_OK "MangaTrans 安装不完整：缺少主程序。请重新下载安装包并校验 SHA-256。"
    mtPostInstallAbort:
    Abort
    !undef MT_INSTALL_CHECK_MESSAGE
  ${EndIf}

  ${IfNot} ${FileExists} "$INSTDIR\mangatrans-backend.exe"
    !define MT_INSTALL_CHECK_MESSAGE "缺少后端 mangatrans-backend.exe；安装包可能不完整"
    !insertmacro MT_WRITE_INSTALL_CHECK_MESSAGE
    IfSilent mtPostInstallBackendAbort
    MessageBox MB_ICONSTOP|MB_OK "MangaTrans 安装不完整：缺少本地翻译服务。请重新下载安装包并校验 SHA-256。"
    mtPostInstallBackendAbort:
    Abort
    !undef MT_INSTALL_CHECK_MESSAGE
  ${EndIf}
!macroend
