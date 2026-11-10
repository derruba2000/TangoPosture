#ifndef AppVersion
  #error AppVersion must be supplied
#endif

[Setup]
AppId={{BAE63F97-77A4-478C-9C3F-218A6E63CD75}
AppName=TangoPosture
AppVersion={#AppVersion}
DefaultDirName={localappdata}\Programs\TangoPosture
DefaultGroupName=TangoPosture
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir=..\release
OutputBaseFilename=TangoPosture-Windows-x64-Setup
Compression=lzma2
SolidCompression=yes
UninstallDisplayIcon={app}\TangoPosture.exe

[Files]
Source: "..\dist\TangoPosture\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\TangoPosture"; Filename: "{app}\TangoPosture.exe"
