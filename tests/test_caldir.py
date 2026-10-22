"""Tests for the caldir backend, driven through a fake caldir CLI."""

import json
import unittest
from datetime import datetime, timezone

from omarchy_calendar_sync import cli, config, normalize
from omarchy_calendar_sync.caldir import Caldir, CaldirError, CaldirMissing

CALENDARS = [
    {"slug": "me-example-com", "name": "me@example.com", "color": "#9fe1e7", "read_only": False},
    {"slug": "room", "name": "Meeting Room ", "color": None, "read_only": True},
]

MEETING = {
    "all_day": False,
    "calendar": "me-example-com",
    "uid": "abc@google.com",
    "instance_id": "abc@google.com",
    "title": "BA/Prod Sync",
    "start": "2026-10-22T09:00:00+01:00",
    "end": "2026-10-22T09:30:00+01:00",
    "status": "confirmed",
    "rsvp": "accepted",
    "location": None,
    "description": None,
    "url": "https://meet.google.com/heh-ztag-znu",
    "reminders": [{"minutes_before_start": 10}],
    "x_properties": [{"name": "X-GOOGLE-CONFERENCE", "params": [], "value": "https://meet.google.com/heh-ztag-znu"}],
}

WORKING_LOCATION = {
    "all_day": True,
    "calendar": "me-example-com",
    "uid": "home@google.com",
    "instance_id": "home@google.com__20261022",
    "title": "Home",
    "start": "2026-10-22",
    "end": "2026-10-23",
    "status": "confirmed",
    "rsvp": None,
    "url": None,
    "reminders": [],
    "x_properties": [{"name": "X-GOOGLE-EVENT-TYPE", "params": [], "value": "workingLocation"}],
}

OUTSIDE = dict(MEETING, uid="late@google.com", start="2027-03-01T09:00:00+00:00", end="2027-03-01T10:00:00+00:00")


class FakeCaldir:
    def __init__(self, events=None, pull_code=0, version="caldir-cli 0.14.8\n"):
        self.calls = []
        self._events = events if events is not None else [MEETING, WORKING_LOCATION, OUTSIDE]
        self._pull_code = pull_code
        self._version = version

    def __call__(self, argv):
        args = argv[1:]
        self.calls.append(args)
        if args == ["--version"]:
            return 0, self._version, ""
        if args == ["pull"]:
            return self._pull_code, "", "network unreachable" if self._pull_code else ""
        if args == ["--json", "config"]:
            return 0, json.dumps({"config": {"default_calendar": "me-example-com"}}), ""
        if args == ["--json", "calendars"]:
            return 0, json.dumps(CALENDARS), ""
        if args[:2] == ["--json", "events"]:
            slug = args[args.index("--calendar") + 1]
            return 0, json.dumps([e for e in self._events if e["calendar"] == slug]), ""
        return 2, "", "unexpected %r" % args


WINDOW = ("2026-10-15T00:00:00+00:00", "2026-12-21T00:00:00+00:00")


class TestCalendars(unittest.TestCase):
    def test_slugs_become_ids_and_default_calendar_is_primary(self):
        found = Caldir(runner=FakeCaldir(), pull=False).calendars()
        by_id = {c["id"]: c for c in found}
        self.assertTrue(by_id["me-example-com"]["primary"])
        self.assertTrue(by_id["me-example-com"]["writable"])
        self.assertFalse(by_id["room"]["primary"])
        self.assertFalse(by_id["room"]["writable"])

    def test_names_are_trimmed_and_missing_colours_filled(self):
        room = [c for c in Caldir(runner=FakeCaldir(), pull=False).calendars() if c["id"] == "room"][0]
        self.assertEqual(room["name"], "Meeting Room")
        self.assertTrue(room["color"].startswith("#"))

    def test_pulls_once_before_reading(self):
        fake = FakeCaldir()
        client = Caldir(runner=fake)
        client.calendars()
        client.calendars()
        self.assertEqual(fake.calls.count(["pull"]), 1)
        self.assertEqual(fake.calls[0], ["pull"])

    def test_no_pull_when_turned_off(self):
        fake = FakeCaldir()
        Caldir(runner=fake, pull=False).calendars()
        self.assertNotIn(["pull"], fake.calls)

    def test_a_failed_pull_still_reads_the_local_copy(self):
        found = Caldir(runner=FakeCaldir(pull_code=1)).calendars()
        self.assertEqual(len(found), 2)


class TestEvents(unittest.TestCase):
    def setUp(self):
        self.events = Caldir(runner=FakeCaldir(), pull=False).events("me-example-com", *WINDOW)

    def test_events_outside_the_window_are_dropped(self):
        self.assertEqual(sorted(e["iCalUID"] for e in self.events), ["abc@google.com", "home@google.com"])

    def test_meeting_link_answer_and_reminders_survive_normalize(self):
        calendar = {"id": "me-example-com", "name": "me", "color": "#000000"}
        meeting = [e for e in self.events if e["iCalUID"] == "abc@google.com"][0]
        rows = normalize.normalize_event(meeting, calendar, timezone.utc)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["meetingUrl"], "https://meet.google.com/heh-ztag-znu")
        self.assertEqual(rows[0]["responseStatus"], "accepted")
        self.assertEqual(rows[0]["reminders"], [10])

    def test_all_day_keeps_its_date_and_google_event_type(self):
        home = [e for e in self.events if e["iCalUID"] == "home@google.com"][0]
        self.assertEqual(home["start"], {"date": "2026-10-22"})
        self.assertEqual(home["end"], {"date": "2026-10-23"})
        self.assertEqual(home["eventType"], "workingLocation")

    def test_a_time_without_an_offset_is_refused(self):
        naive = dict(MEETING, start="2026-10-22T09:00:00", end="2026-10-22T09:30:00")
        client = Caldir(runner=FakeCaldir(events=[naive]), pull=False)
        with self.assertRaises(CaldirError):
            client.events("me-example-com", *WINDOW)


class TestErrors(unittest.TestCase):
    def test_missing_binary_names_the_config_key(self):
        def runner(_argv):
            raise FileNotFoundError

        with self.assertRaises(CaldirMissing) as caught:
            Caldir(runner=runner).check()
        self.assertIn("caldirPath", str(caught.exception))

    def test_unparseable_version_is_an_error(self):
        with self.assertRaises(CaldirError):
            Caldir(runner=FakeCaldir(version="")).version()


class TestWiring(unittest.TestCase):
    def test_config_selects_the_backend(self):
        cfg = config._merge(dict(config.DEFAULTS), {"backend": "caldir", "caldirPull": False})
        client = cli.build_client(cfg)
        self.assertIsInstance(client, Caldir)
        self.assertEqual(client.binary, "caldir")

    def test_a_full_run_writes_a_valid_document(self):
        import tempfile
        from pathlib import Path

        cfg = config._merge(dict(config.DEFAULTS), {"backend": "caldir", "calendars": {"include": ["me-example-com"]}})
        client = Caldir(runner=FakeCaldir(), pull=False)
        now = datetime(2026, 10, 22, 12, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as folder:
            out = Path(folder) / "events.json"
            self.assertEqual(cli.run(client, cfg, now, out, timezone.utc), cli.EXIT_OK)
            doc = json.loads(out.read_text())
        self.assertTrue(doc["source"].startswith("caldir/0.14.8"))
        self.assertEqual({row["calendarId"] for row in doc["events"]}, {"me-example-com"})


if __name__ == "__main__":
    unittest.main()
