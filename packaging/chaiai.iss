; chaiai Windows 安装包（Inno Setup 6）。
;
; 定位：把 `local-agent-web/scripts/build_windows_package.py` 产出的**目录式包**
; （`local-agent-web/build/packaging/dist/chaiai/`）封成一个单文件安装程序。两者职责分开，不要混：
;   - 构建脚本管"包里有什么"（前端产物、DLL 补齐、敏感数据扫描、MANIFEST/BUILD_INFO）；
;   - 本脚本只管"装到哪、怎么卸、留什么"。
;
; 三条约定（改之前先读）：
; 1. **按用户安装**（`PrivilegesRequired=lowest`）：装进 `%LOCALAPPDATA%\Programs\chaiai`，
;    不需要管理员权限。程序本身也不写 HKCU 以外的地方。
; 2. **用户数据不归安装包管**：配置、SQLite、记忆都在 `%LOCALAPPDATA%\chaiai`（见 app_paths.py），
;    与安装目录无关。因此卸载**不删**那个目录——删数据必须是用户自己的动作。
; 3. **升级＝再装一次**：AppId 固定，新版本直接覆盖安装；`CloseApplications=yes` 会在卸载/覆盖
;    前提示关闭正在运行的 chaiai.exe（Windows 不允许覆盖运行中的 exe）。
;
; 编译（在**仓库根**下；所有相对路径都以本文件所在目录为基准）：
;   ISCC.exe /DAppVersion=0.1.0 /DSourceDir=..\local-agent-web\build\packaging\dist\chaiai packaging\chaiai.iss
; 产物：local-agent-web/build/packaging/installer/chaiai-setup-<version>.exe

#define AppName "chaiai"
#define AppExeName "chaiai.exe"

; 版本号与源目录都由命令行传入，避免在脚本里写第二份真相；
; 直接双击编译时退回一组能跑通的默认值。
#ifndef AppVersion
  #define AppVersion "0.0.0-dev"
#endif
#ifndef SourceDir
  #define SourceDir "..\local-agent-web\build\packaging\dist\chaiai"
#endif

#define AppPublisher "chaiai contributors"
#define AppURL "https://gitee.com/the-old-man-loves/chaiai"

[Setup]
; AppId 必须永远不变：它决定"这是同一个应用的新版本"还是"另一个应用"。
AppId={{7C1E4B60-9D2A-4D2E-9A4E-5E1B2C3D4F50}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher={#AppPublisher}
AppPublisherURL={#AppURL}
AppSupportURL={#AppURL}
AppUpdatesURL={#AppURL}

; 按用户安装：不需要 UAC，卸载也不留系统级残留。
DefaultDirName={localappdata}\Programs\chaiai
DefaultGroupName={#AppName}
; 允许用户改安装位置（`no` = 显示目录页）。注意：按用户安装时不要选 Program Files，
; 那里没有写权限，装到那里会失败——所以默认值给的是用户目录。
DisableDirPage=no
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=

; 产物
OutputDir=..\local-agent-web\build\packaging\installer
OutputBaseFilename=chaiai-setup-{#AppVersion}
SetupIconFile=..\local-agent-web\frontend\public\chaiai.ico
UninstallDisplayIcon={app}\{#AppExeName}
UninstallDisplayName={#AppName}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
; 覆盖安装前提示关闭正在运行的程序（Windows 不能替换运行中的 exe）。
CloseApplications=yes
RestartApplications=no
; 显示许可，与仓库根 LICENSE 一致。
LicenseFile=..\LICENSE

[Languages]
; 官方发行包不带简体中文语言文件（Unofficial/ChineseSimplified.isl 需单独提供）。
; 这里用默认英文做骨架，再在 [Messages] 里把用户真正会读的几句改成中文；
; 等拿到官方中文 isl 后整体替换即可（TODO 记在 packaging/README 里）。
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "附加快捷方式:"; Flags: unchecked

[Files]
; 整目录装进去。excludes 只挡两种东西：构建中间目录与用户数据（万一被打进包里）。
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExeName}"; WorkingDir: "{app}"
Name: "{group}\卸载 {#AppName}"; Filename: "{uninstallexe}"
Name: "{userdesktop}\{#AppName}"; Filename: "{app}\{#AppExeName}"; WorkingDir: "{app}"; Tasks: desktopicon

[Run]
; 装完直接启动；`postinstall nowait` 让安装向导不等着它退出（它是常驻服务）。
Filename: "{app}\{#AppExeName}"; Description: "立即启动 {#AppName}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; 只删安装目录里的运行时残留（启动日志可能落在 exe 同级）。
; **不要**在这里加 {localappdata}\chaiai：那是用户数据目录，卸载不删。
Type: files; Name: "{app}\启动错误.log"

[Messages]
; 用户真正会读到的几句。键名来自 Inno 官方 Default.isl，写错会在编译时报 Unknown message name。
SetupAppTitle={#AppName} 安装程序
SetupWindowTitle={#AppName} {#AppVersion} 安装程序
WelcomeLabel1=欢迎安装 [name]
WelcomeLabel2=将在这台电脑上安装 [name/ver]。%n%n安装到你的用户目录，不需要管理员权限；会话、设置与记忆保存在 %n%LOCALAPPDATA%\chaiai，卸载时不会被删除。
SelectDirLabel3=安装程序会把 [name] 装到下面的文件夹。
SelectDirDesc=[name] 要装到哪里？
SelectDirBrowseLabel=点"下一步"继续；要换位置点"浏览"（建议保持默认的用户目录，那里不需要管理员权限）。
DiskSpaceMBLabel=至少需要 [mb] MB 可用磁盘空间。
SelectTasksDesc=还要做哪些额外设置？
SelectTasksLabel2=选择安装 [name] 时要一并完成的事情，然后点"下一步"。
ReadyLabel1=准备就绪，可以开始安装 [name]。
ReadyLabel2a=点"安装"开始；想再核对一遍设置就点"上一步"。
ReadyLabel2b=点"安装"开始。
ReadyMemoDir=安装位置：
ReadyMemoGroup=开始菜单文件夹：
ReadyMemoTasks=附加任务：
PreparingDesc=正在准备安装 [name]…
ClickFinish=点"完成"退出安装程序。
FinishedRestartLabel=要完成 [name] 的安装需要重启电脑，现在重启吗？
InstallingLabel=正在安装 [name]，请稍候…
FinishedHeadingLabel=[name] 安装完成
FinishedLabel=安装完成。可以从开始菜单或桌面快捷方式启动 [name]。%n%n卸载不会删除你的会话与设置（它们在 %LOCALAPPDATA%\chaiai）。
FinishedLabelNoIcons=安装完成。%n%n卸载不会删除你的会话与设置（它们在 %LOCALAPPDATA%\chaiai）。
ConfirmUninstall=确定要卸载 %1 吗？%n%n你的会话、设置与记忆保存在 %LOCALAPPDATA%\chaiai，不会被删除。
UninstalledAll=%1 已卸载。
UninstalledMost=%1 卸载完成。%n%n有个别文件没能删除，可以手动清理安装目录。
ClickNext=下一步
ClickInstall=安装
ClickFinish=完成
ButtonNext=下一步(&N)
ButtonBack=上一步(&B)
ButtonInstall=安装(&I)
ButtonCancel=取消
ButtonFinish=完成(&F)
ButtonBrowse=浏览(&R)…
