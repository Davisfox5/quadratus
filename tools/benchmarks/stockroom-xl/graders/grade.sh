#!/bin/sh
# grade.sh PROJECT OUT.json : run the hidden graders against a finished project.
G=$(cd "$(dirname "$0")" && pwd)
cd "$1" || exit 2
env -u HTTP_PROXY -u HTTPS_PROXY -u http_proxy -u https_proxy PATH=/usr/local/bin:/usr/bin:/bin \
  python -m pytest "$G" -q -p no:cacheprovider -c /dev/null --rootdir "$G" -o cache_dir=/dev/null \
  --junitxml="$2.xml" > "$2.txt" 2>&1
python3 - "$2" <<'P'
import json, sys, xml.etree.ElementTree as ET
r = ET.parse(sys.argv[1] + ".xml").getroot()
s = r if r.tag == "testsuite" else r[0]
total = int(s.get("tests")); bad = int(s.get("failures")) + int(s.get("errors")) + int(s.get("skipped"))
json.dump({"passed": total - bad, "total": total}, open(sys.argv[1], "w"))
print(open(sys.argv[1]).read())
P
