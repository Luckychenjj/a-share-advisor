#ifndef AppSource
  #error AppSource is required
#endif
#ifndef OutputDir
  #error OutputDir is required
#endif
#ifndef AppVersion
  #define AppVersion "1.0.8"
#endif

[Setup]
AppId={{9D3B21AC-6E88-46D8-AF4D-1A4B83CFB62F}
AppName=小A顾问
AppVersion={#AppVersion}
AppVerName=小A顾问 {#AppVersion}
AppPublisher=小A顾问
DefaultDirName={autopf}\XiaoAAdvisor
DefaultGroupName=小A顾问
DisableProgramGroupPage=yes
DisableDirPage=no
UsePreviousAppDir=no
PrivilegesRequired=admin
SetupArchitecture=x64
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir={#OutputDir}
OutputBaseFilename=小A顾问{#AppVersion}
SetupIconFile={#AppSource}\app_icon.ico
UninstallDisplayName=小A顾问 {#AppVersion}
Compression=lzma2/ultra64
SolidCompression=yes
WizardStyle=modern
CloseApplications=yes
RestartApplications=no
Uninstallable=yes

[Languages]
Name: "chinesesimplified"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "快捷方式："; Flags: unchecked

[Files]
Source: "{#AppSource}\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion

[Icons]
Name: "{autoprograms}\小A顾问"; Filename: "{app}\xiao_a_client.exe"; WorkingDir: "{app}"
Name: "{autodesktop}\小A顾问"; Filename: "{app}\xiao_a_client.exe"; WorkingDir: "{app}"; Tasks: desktopicon

[Run]
Filename: "{app}\xiao_a_client.exe"; Description: "启动小A顾问"; Flags: postinstall nowait skipifsilent

[UninstallDelete]
; Per-user portfolio/cache under LocalAppData is deliberately preserved.
