"""Politique PowerShell : lecture libre, modification confirmée, interdits refusés.

Lance un PowerShell par script analysé (~0,5 s chacun) : python -m unittest tests.test_shell
"""

import unittest

from jarvis.tools.shell import analyze, run_powershell

ALLOW = [
    r"Get-Process | Sort-Object CPU -Descending | Select-Object -First 5 Name, CPU",
    r"Get-ChildItem $env:USERPROFILE\Desktop | Select Name, Length",
    r"(Get-CimInstance Win32_OperatingSystem).FreePhysicalMemory / 1MB",
    r"[math]::Round((Get-PSDrive C).Free / 1GB, 1)",
    r"Get-NetIPAddress -AddressFamily IPv4 | ? { $_.InterfaceAlias -notlike '*Loopback*' } | % IPAddress",
    r"ipconfig /all",
    r"Get-ChildItem C:\temp 2>$null",
    r"(Get-Item C:\Windows).GetFiles().Count",
]
CONFIRM = [
    r"Start-Process notepad",
    r"Stop-Process -Name chrome",
    r"Copy-Item a.txt b.txt",
    r"Invoke-WebRequest https://example.com",
    r"Set-Clipboard -Value hello",
    r"New-Item -ItemType File note.txt",
]
FORBID = [
    r"Remove-Item C:\temp\x.txt",
    r"rm -r C:\temp",
    r"del x.txt",
    r"Get-ChildItem C:\temp | ForEach-Object { $_.Delete() }",
    r"[IO.File]::Delete('x')",
    r"Stop-Computer",
    r"shutdown /s /t 0",
    r"Restart-Computer -Force",
    r"Set-ItemProperty HKCU:\Software\x -Name a -Value 1",
    r"iex 'Remove-Item x'",
    r"& ('Remove' + '-Item') x",
    r"Start-Process cmd -ArgumentList '/c del x'",
    r"Start-Process powershell -Verb RunAs",
    r"(New-Object -ComObject Shell.Application).ShutdownWindows()",
    r"Get-WmiObject Win32_OperatingSystem | % { $_.Win32Shutdown(1) }",
    r"net user Dan nouveau",
    r"Copy-Item a b -Force",
    r"Get-Process > procs.txt",
    r"Get-Process | Out-File p.txt",
    r"Clear-RecycleBin -Force",
    r"winget install vlc",
    r"$m = 'Delete'; $f.$m()",
    r"[Environment]::SetEnvironmentVariable('A', 'B', 'User')",
    r"Get-Process (",
    r"$s = New-Object -ComObject Shell.Application; $s.NameSpace(10).Self.InvokeVerb('Empty Recycle &Bin')",
    r"(New-Object -ComObject Shell.Application).ShellExecute('cmd.exe', '/c del C:\temp\x.txt')",
    r"(New-Object -ComObject WScript.Shell).Run('cmd /c del x')",
]


class PolicyTest(unittest.TestCase):
    def _check(self, scripts, expected):
        for script in scripts:
            with self.subTest(script=script):
                verdict, reason = analyze(script)
                self.assertEqual(verdict.value, expected, reason)

    def test_read_scripts_run_directly(self):
        self._check(ALLOW, "allow")

    def test_changes_need_confirmation(self):
        self._check(CONFIRM, "confirm")

    def test_red_lines_are_refused(self):
        self._check(FORBID, "forbid")


class RunTest(unittest.TestCase):
    def test_output(self):
        result = run_powershell("'bon' + 'jour'", "test")
        self.assertEqual((result.ok, result.message), (True, "bonjour"))

    def test_errors_are_reported(self):
        result = run_powershell("Get-PSDrive -Name 'Z:\\inexistant'", "test")
        self.assertFalse(result.ok)
        self.assertIn("Erreurs PowerShell", result.message)

    def test_partial_errors_keep_output(self):
        result = run_powershell("'avant'; Get-Item 'C:\\inexistant_jarvis'; 'après'", "test")
        self.assertTrue(result.ok)
        self.assertIn("après", result.message)
        self.assertIn("Erreurs PowerShell", result.message)


if __name__ == "__main__":
    unittest.main()
