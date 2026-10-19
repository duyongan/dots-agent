; dots 的 Windows 安装包。用 Inno Setup 6 编译：
;   ISCC.exe packaging\dots.iss
; 产物：release\dots-setup-<版本>.exe

#define MyAppName "dots"
#define MyAppVersion "1.1.1"
#define MyAppPublisher "duyongan"
#define MyAppURL "https://github.com/duyongan/dots-agent"
#define MyAppExeName "dots.exe"

[Setup]
AppId={{8D0C2B3A-4F1E-4A5C-9B7D-D0C5A1E2F3B4}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppURL}
AppUpdatesURL={#MyAppURL}
DefaultDirName={localappdata}\Programs\dots
DefaultGroupName=dots
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir=..\release
OutputBaseFilename=dots-setup-{#MyAppVersion}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ChangesEnvironment=yes
UninstallDisplayName={#MyAppName}

[Tasks]
Name: "addtopath"; Description: "把 dots 加入 PATH（推荐，之后在任何终端直接敲 dots）"; GroupDescription: "环境变量："; Flags: checkedonce

[Files]
Source: "..\dist\dots\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\dots（网页界面）"; Filename: "{app}\dots-gui.exe"; WorkingDir: "{app}"
Name: "{group}\dots（命令行）"; Filename: "{app}\{#MyAppExeName}"; WorkingDir: "{app}"
Name: "{group}\卸载 dots"; Filename: "{uninstallexe}"

[Run]
Filename: "{app}\dots-gui.exe"; Description: "打开 dots 界面"; WorkingDir: "{app}"; Flags: postinstall nowait skipifsilent

[Registry]
Root: HKCU; Subkey: "Environment"; ValueType: expandsz; ValueName: "Path"; ValueData: "{app};{olddata}"; Tasks: addtopath; Check: NeedsAddPath(ExpandConstant('{app}'))

[Code]
function NeedsAddPath(Param: string): Boolean;
var
  PathStr: string;
begin
  if not RegQueryStringValue(HKCU, 'Environment', 'Path', PathStr) then
  begin
    Result := True;
    Exit;
  end;
  Result := Pos(';' + Uppercase(Param) + ';', ';' + Uppercase(PathStr) + ';') = 0;
end;

function RemovePathEntry(PathStr, Entry: string): string;
var
  Parts: TStringList;
  I: Integer;
  Found: Boolean;
begin
  Result := '';
  Parts := TStringList.Create;
  try
    Parts.Delimiter := ';';
    Parts.StrictDelimiter := True;
    Parts.DelimitedText := PathStr;
    Found := False;
    for I := 0 to Parts.Count - 1 do
    begin
      if (not Found) and (CompareText(Parts[I], Entry) = 0) then
        Found := True
      else
      begin
        if Result <> '' then
          Result := Result + ';';
        Result := Result + Parts[I];
      end;
    end;
  finally
    Parts.Free;
  end;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  PathStr: string;
begin
  if CurUninstallStep <> usPostUninstall then
    Exit;
  if RegQueryStringValue(HKCU, 'Environment', 'Path', PathStr) then
    RegWriteExpandStringValue(HKCU, 'Environment', 'Path',
      RemovePathEntry(PathStr, ExpandConstant('{app}')));
end;
