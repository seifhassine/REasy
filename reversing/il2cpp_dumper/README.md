# RE Engine IL2CPP Dumper

Standalone dumper for RE Engine IL2CPP metadata.

## Supported games

Targets 64-bit RE Engine releases from  *Devil May Cry 5* through *Pragmata*.

Tested on *Resident Evil 2*, *Devil May Cry 5*, *Resident Evil 3*, *Resident Evil Village*, *Resident Evil 4*, *Dragon's Dogma 2*, *Onimusha 2: Samurai's Destiny*, *Monster Hunter Wilds*, *Monster Hunter Stories 3: Twisted Reflection*, *Resident Evil Requiem*, and *Pragmata*.

*Resident Evil 7* non-RT is  not supported.

## Requirements

- Windows and PowerShell 7+
- The game running at the title screen

## Usage

```powershell
pwsh -NoProfile -File "D:\RE Modding\REasy\reversing\il2cpp_dumper\dump_il2cpp.ps1" `
  -GamePath "D:\Path\To\Game.exe" `
  -OutputPath ".\il2cpp_dump.json" `
  -Force
```

Run `Get-Help .\dump_il2cpp.ps1 -Full` for all options.
