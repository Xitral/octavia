# One page of a PDF as a PNG picture, with Windows' own PDF renderer (Windows.Data.Pdf): no extra install.
# Used by src/octavia/tools/extensions/_pdf_pages.py for slides that are pictures (or diagrams) rather than text.
# Prints "pages=<count>" and then "ok" once the picture is written; page numbers start at 1.
param([Parameter(Mandatory)][string]$Pdf, [Parameter(Mandatory)][int]$Page, [Parameter(Mandatory)][string]$Out,
      [int]$Width = 1600)
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Runtime.WindowsRuntime
$methods = [System.WindowsRuntimeSystemExtensions].GetMethods()
$asTask = $methods | Where-Object {
    $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and
    $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1' } | Select-Object -First 1
$asAction = $methods | Where-Object {
    $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and
    $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncAction' } | Select-Object -First 1
function Await($operation, [type]$resultType) {
    $task = $asTask.MakeGenericMethod($resultType).Invoke($null, @($operation))
    $task.Wait()
    $task.Result
}
[void][Windows.Storage.StorageFile, Windows.Storage, ContentType = WindowsRuntime]
[void][Windows.Data.Pdf.PdfDocument, Windows.Data.Pdf, ContentType = WindowsRuntime]
[void][Windows.Storage.Streams.InMemoryRandomAccessStream, Windows.Storage.Streams, ContentType = WindowsRuntime]

$file = Await ([Windows.Storage.StorageFile]::GetFileFromPathAsync($Pdf)) ([Windows.Storage.StorageFile])
$doc = Await ([Windows.Data.Pdf.PdfDocument]::LoadFromFileAsync($file)) ([Windows.Data.Pdf.PdfDocument])
"pages=$($doc.PageCount)"
if ($Page -lt 1 -or $Page -gt $doc.PageCount) { exit 3 }
$pdfPage = $doc.GetPage($Page - 1)
$options = New-Object Windows.Data.Pdf.PdfPageRenderOptions
$options.DestinationWidth = $Width
$stream = New-Object Windows.Storage.Streams.InMemoryRandomAccessStream
$asAction.Invoke($null, @($pdfPage.RenderToStreamAsync($stream, $options))).Wait()
$source = [System.IO.WindowsRuntimeStreamExtensions]::AsStreamForRead($stream.GetInputStreamAt(0))
$output = [System.IO.File]::Create($Out)
try { $source.CopyTo($output) } finally { $output.Close() }
"ok"
