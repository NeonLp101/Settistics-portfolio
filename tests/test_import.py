import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"pipeline"))
from import_aggregate import main, normalize_bucket

class AggregateImportTests(unittest.TestCase):
    def bucket(self):
        return {"champion":"Sett","opponent":"Teemo","role":"TOP","patch":"16.18","region":"GLOBAL",
                "games":100,"wins":48,"eligible":{"items":100},
                "choices":[{"kind":"items","id":"1054","label":"Doran's Shield","games":80,"wins":40}]}

    def test_normalizes_counts_without_creating_wpa(self):
        value=normalize_bucket(self.bucket())
        self.assertEqual(value["games"],100)
        self.assertNotIn("wpa",value)
        self.assertEqual(value["choices"][0]["wins"],40)

    def test_rejects_impossible_choice_denominator(self):
        value=self.bucket();value["choices"][0]["games"]=101
        with self.assertRaises(ValueError):normalize_bucket(value)

    def test_cli_requires_reuse_confirmation(self):
        with tempfile.TemporaryDirectory() as directory:
            source=Path(directory)/"input.json";source.write_text(json.dumps([self.bucket()]),encoding="utf-8")
            with self.assertRaises(SystemExit):main(["--input",str(source),"--provider-id","provider","--provider-name","Provider","--source-url","https://example.test/data"])

    def test_writes_valid_provider_envelope(self):
        with tempfile.TemporaryDirectory() as directory:
            source=Path(directory)/"input.json";output=Path(directory)/"output.json"
            source.write_text(json.dumps([self.bucket()]),encoding="utf-8")
            main(["--input",str(source),"--provider-id","provider","--provider-name","Provider","--source-url","https://example.test/data","--rights-confirmed","--output",str(output)])
            value=json.loads(output.read_text(encoding="utf-8"))
            self.assertTrue(value["rightsConfirmed"]);self.assertEqual(value["provider"]["id"],"provider")
            self.assertNotIn("wpa",json.dumps(value).lower())

if __name__=="__main__":unittest.main()
