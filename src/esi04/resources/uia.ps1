$ErrorActionPreference = 'Stop'
[Console]::InputEncoding = New-Object System.Text.UTF8Encoding($false)
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
$request = [Console]::In.ReadToEnd() | ConvertFrom-Json
$root = [System.Windows.Automation.AutomationElement]::FromHandle([IntPtr][long]$request.handle)
$nodes = $root.FindAll([System.Windows.Automation.TreeScope]::Descendants, [System.Windows.Automation.Condition]::TrueCondition)
$result = New-Object System.Collections.Generic.List[object]
for ($index = 0; $index -lt $nodes.Count -and $result.Count -lt 256; $index++) {
    try {
        $node = $nodes.Item($index)
        $current = $node.Current
        $rect = $current.BoundingRectangle
        if ($current.IsOffscreen -or $rect.Width -le 0 -or $rect.Height -le 0) { continue }
        $value = ''
        if (-not $current.IsPassword) {
            $pattern = $null
            if ($node.TryGetCurrentPattern([System.Windows.Automation.ValuePattern]::Pattern, [ref]$pattern)) {
                $value = [string]$pattern.Current.Value
                if ($value.Length -gt 4000) { $value = $value.Substring(0,4000) }
            }
        }
        $name = [string]$current.Name
        if ($name.Length -gt 500) { $name = $name.Substring(0,500) }
        $id = [string]$current.AutomationId
        if ($id.Length -gt 300) { $id = $id.Substring(0,300) }
        $result.Add(@{ name=$name; role=[string]$current.ControlType.ProgrammaticName; automation_id=$id;
            bounds=@{x=[int]$rect.X;y=[int]$rect.Y;width=[int]$rect.Width;height=[int]$rect.Height};
            value=$value;protected=[bool]$current.IsPassword;enabled=[bool]$current.IsEnabled;
            focused=[bool]$current.HasKeyboardFocus })
    } catch [System.Windows.Automation.ElementNotAvailableException] { continue }
}
@{elements=@($result.ToArray())} | ConvertTo-Json -Depth 5 -Compress
