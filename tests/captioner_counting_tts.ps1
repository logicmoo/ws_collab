param(
    [Parameter(Mandatory=$true)][string]$OutputDirectory,
    [string]$Voice = "Microsoft David Desktop",
    [int]$SampleRate = 16000
)
$ErrorActionPreference = "Stop"
Add-Type -AssemblyName System.Speech
New-Item -ItemType Directory -Force -Path $OutputDirectory | Out-Null
$synth = New-Object System.Speech.Synthesis.SpeechSynthesizer
try {
    $synth.SelectVoice($Voice)
    $synth.Rate = 0
    $synth.Volume = 100
    $format = New-Object System.Speech.AudioFormat.SpeechAudioFormatInfo(
        $SampleRate,
        [System.Speech.AudioFormat.AudioBitsPerSample]::Sixteen,
        [System.Speech.AudioFormat.AudioChannel]::Mono
    )
    $sources = @(
        @{id="one"; text="one"}, @{id="two"; text="two"},
        @{id="three"; text="three"}, @{id="four"; text="four"},
        @{id="five"; text="five"}, @{id="six"; text="six"},
        @{id="first_phrase"; text="one two three"},
        @{id="second_phrase"; text="four five six"},
        @{id="continuous"; text="one two three four five six"}
    )
    foreach ($source in $sources) {
        $file = Join-Path $OutputDirectory ($source.id + ".wav")
        $synth.SetOutputToWaveFile($file, $format)
        $synth.Speak($source.text)
        $synth.SetOutputToNull()
    }
    @{
        engine = "Windows System.Speech.Synthesis.SpeechSynthesizer"
        voice = $synth.Voice.Name
        culture = $synth.Voice.Culture.Name
        rate = $synth.Rate
        volume = $synth.Volume
        sample_rate = $SampleRate
        sources = $sources
    } | ConvertTo-Json -Depth 5 | Set-Content -Encoding UTF8 (Join-Path $OutputDirectory "synthesis.json")
} finally {
    $synth.Dispose()
}
