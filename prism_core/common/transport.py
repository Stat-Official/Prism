"""HTTPS transport; Windows uses the OS certificate verifier, not OpenSSL."""
from __future__ import annotations

import base64
import json
import os
import subprocess
from urllib.error import HTTPError
from urllib.request import Request

PS_REQUEST = r"""$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
Add-Type -AssemblyName System.Net.Http
$taskSpec = ConvertFrom-Json $env:NEWS_PILOT_REQUEST
$taskHandler = [System.Net.Http.HttpClientHandler]::new()
$taskHandler.AllowAutoRedirect = $false
$taskClient = [System.Net.Http.HttpClient]::new($taskHandler)
$taskClient.Timeout = [TimeSpan]::FromSeconds($taskSpec.timeout)
$taskRequest = [System.Net.Http.HttpRequestMessage]::new([System.Net.Http.HttpMethod]::Get, [string]$taskSpec.url)
foreach ($entry in $taskSpec.headers.PSObject.Properties) {
    [void]$taskRequest.Headers.TryAddWithoutValidation($entry.Name, [string]$entry.Value)
}
try {
    $taskResponse = $taskClient.SendAsync($taskRequest).GetAwaiter().GetResult()
    $taskBytes = $taskResponse.Content.ReadAsByteArrayAsync().GetAwaiter().GetResult()
    @{
        status = [int]$taskResponse.StatusCode
        url = $taskSpec.url
        content_type = [string]$taskResponse.Content.Headers.ContentType
        location = [string]$taskResponse.Headers.Location
        body = [Convert]::ToBase64String($taskBytes)
    } | ConvertTo-Json -Compress
} catch {
    @{status=0; error=$_.Exception.GetBaseException().Message} | ConvertTo-Json -Compress
} finally {
    $taskClient.Dispose()
    $taskHandler.Dispose()
}
"""


def fetch(url: str, headers: dict[str, str] | None = None, timeout: int = 25) -> dict:
    """No automatic redirect: callers decide whether forwarding is appropriate."""
    headers = headers or {}
    if os.name == 'nt':
        environment = os.environ.copy()
        environment['NEWS_PILOT_REQUEST'] = json.dumps(
            {'url': url, 'headers': headers, 'timeout': timeout}, ensure_ascii=True
        )
        process = subprocess.run(
            ['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', PS_REQUEST],
            env=environment, capture_output=True, timeout=timeout + 15,
        )
        if process.returncode:
            raise ConnectionError('Windows HTTPS transport failed: ' +
                                  process.stderr.decode('utf-8', errors='replace')[:500])
        response = json.loads(process.stdout.decode('utf-8-sig'))
        if not response['status']:
            raise ConnectionError(response.get('error', 'HTTPS transport failed'))
        response['body'] = base64.b64decode(response['body'])
        return response
    # Other platforms use standard Python certificate/hostname verification
    # with redirects disabled.
    from urllib.request import HTTPRedirectHandler, build_opener

    class NoRedirect(HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, response_headers, newurl):
            return None

    try:
        response = build_opener(NoRedirect()).open(Request(url, headers=headers), timeout=timeout)
    except HTTPError as exc:
        response = exc
    with response:
        return {'status': response.code, 'url': url,
                'content_type': response.headers.get('Content-Type', ''),
                'location': response.headers.get('Location', ''), 'body': response.read()}
