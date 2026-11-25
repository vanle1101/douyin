"""Keep Python PiperVoice loaded for successive JSON-lines requests."""
import json, pathlib, sys, wave
from piper import PiperVoice

voice=PiperVoice.load(sys.argv[1])
for line in sys.stdin:
    if not line.strip(): continue
    item=json.loads(line)
    target=pathlib.Path(item['output_file'])
    temp=target.with_suffix('.part')
    with wave.open(str(temp),'wb') as output:
        voice.synthesize(item['text'], output)
    temp.replace(target)
