"""Unit tests for scripts/voice_notify.py. Standard library only: python3 -m unittest discover -s tests"""
import importlib
import io
import json
import os
import random
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))


class StateTestCase(unittest.TestCase):
    """Each test gets its own state folder, so nothing touches ~/.claude/voice-notify."""

    def setUp(self):
        self.home = tempfile.mkdtemp()
        os.environ["CLAUDE_VOICE_NOTIFY_HOME"] = self.home
        import voice_notify
        self.vn = importlib.reload(voice_notify)
        self.env = mock.patch.dict(os.environ, {"CLAUDE_CODE_ENTRYPOINT": "cli"})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        os.environ.pop("CLAUDE_VOICE_NOTIFY_HOME", None)
        shutil.rmtree(self.home, ignore_errors=True)


class SpokenSummaryTest(unittest.TestCase):
    def setUp(self):
        import voice_notify
        self.summary = voice_notify.spoken_summary

    def test_no_line_means_no_summary(self):
        self.assertEqual(self.summary("All done, tests pass."), "")
        self.assertEqual(self.summary(""), "")
        self.assertEqual(self.summary(None), "")

    def test_only_at_the_start_of_a_line(self):
        self.assertEqual(self.summary("The 🔊 icon means sound.\nBye."), "")
        self.assertEqual(self.summary("Report.\n🔊 Tests pass. Please review."), "Tests pass. Please review.")

    def test_the_last_line_wins(self):
        text = "🔊 First draft.\nMore text.\n🔊 Final summary."
        self.assertEqual(self.summary(text), "Final summary.")

    def test_markdown_prefixes_and_variation_selector(self):
        self.assertEqual(self.summary("> 🔊 Quoted."), "Quoted.")
        self.assertEqual(self.summary("- 🔊 Listed."), "Listed.")
        self.assertEqual(self.summary("**🔊** Bold."), "Bold.")
        self.assertEqual(self.summary("🔊️: With selector."), "With selector.")

    def test_markdown_and_links_are_removed(self):
        text = "🔊 Fixed **the** `parser`, see [the PR](https://example.com/pr/1) and https://example.com/x."
        self.assertEqual(self.summary(text), "Fixed the parser, see the PR and")

    def test_runs_to_the_end_of_the_reply(self):
        self.assertEqual(self.summary("🔊 One.\nTwo."), "One. Two.")

    def test_limit_cuts_at_a_word(self):
        out = self.summary("🔊 " + "word, " * 200, limit=50)
        self.assertLessEqual(len(out), 51)
        self.assertTrue(out.endswith("word."), out)


class AnnouncementTest(unittest.TestCase):
    def setUp(self):
        import voice_notify
        self.vn = voice_notify
        self.rng = random.Random(1)

    def say(self, event, payload, lang="en", agent=None):
        return self.vn.announcement(event, payload, "my app", lang, self.rng, agent=agent)

    def test_stop_with_summary_speaks_only_the_agents_line(self):
        out = self.say("Stop", {"last_assistant_message": "Done.\n🔊 Tests pass. Approve the push."})
        self.assertEqual(out, "Tests pass. Approve the push.")
        out = self.say("Stop", {"last_assistant_message": "Ok.\n🔊 Terminei a landing."}, "pt-BR")
        self.assertEqual(out, "Terminei a landing.")

    def test_stop_with_summary_ignores_a_remembered_name(self):
        out = self.say("Stop", {"last_assistant_message": "🔊 Alex, this is Orion. All done."}, agent=("", "Orion"))
        self.assertEqual(out, "Alex, this is Orion. All done.")

    def test_stop_without_summary(self):
        out = self.say("Stop", {"last_assistant_message": "Done."})
        self.assertIn(out, [p.format(p="my app") for p in self.vn.PHRASES["en"]["done"]])

    def test_permission_and_question(self):
        out = self.say("Notification", {"notification_type": "permission_prompt", "message": "x"})
        self.assertIn(out, [p.format(p="my app") for p in self.vn.PHRASES["en"]["approval"]])
        out = self.say("Notification", {"notification_type": "elicitation_dialog"})
        self.assertEqual(out, "my app has a question for you.")

    def test_older_payload_without_type_uses_the_message(self):
        out = self.say("Notification", {"message": "Claude needs your permission to use Bash"})
        self.assertIn(out, [p.format(p="my app") for p in self.vn.PHRASES["en"]["approval"]])

    def test_idle_and_unknown_stay_quiet(self):
        self.assertIsNone(self.say("Notification", {"notification_type": "idle_prompt"}))
        self.assertIsNone(self.say("SubagentStop", {}))

    def test_portuguese(self):
        out = self.say("Notification", {"notification_type": "elicitation_dialog"}, "pt-BR")
        self.assertEqual(out, "my app tem uma pergunta pra você.")

    def test_remembered_name_replaces_the_repository_in_portuguese(self):
        ask = {"notification_type": "permission_prompt"}
        self.assertIn(self.say("Notification", ask, "pt-BR", agent=("o", "Orion")),
                      ["O Orion precisa de você.", "O Orion está esperando sua aprovação."])
        out = self.say("Notification", {"notification_type": "elicitation_dialog"}, "pt-BR", agent=("a", "Luna"))
        self.assertEqual(out, "A Luna tem uma pergunta pra você.")
        out = self.say("Stop", {"last_assistant_message": "Done."}, "pt-BR", agent=("a", "Luna"))
        self.assertIn(out, ["A Luna terminou.", "Pronto, A Luna acabou.", "A Luna terminou, dá uma olhada quando puder."])
        self.assertNotIn("my app", out)

    def test_remembered_name_replaces_the_repository_in_english(self):
        ask = {"notification_type": "permission_prompt"}
        self.assertIn(self.say("Notification", ask, agent=("", "Orion")),
                      ["Orion needs you.", "Orion is waiting for your approval."])
        out = self.say("Notification", {"notification_type": "elicitation_dialog"}, agent=("", "Luna"))
        self.assertEqual(out, "Luna has a question for you.")
        out = self.say("Stop", {"last_assistant_message": "Done."}, agent=("", "Luna"))
        self.assertIn(out, ["Luna is done.", "Luna finished.", "Luna is done, take a look when you can."])

    def test_a_name_from_the_other_language_has_no_article(self):
        out = self.say("Notification", {"notification_type": "elicitation_dialog"}, "pt-BR", agent=("", "Luna"))
        self.assertEqual(out, "Luna tem uma pergunta pra você.")

    def test_without_a_name_the_repository_is_still_said(self):
        out = self.say("Notification", {"notification_type": "elicitation_dialog"}, "pt-BR")
        self.assertEqual(out, "my app tem uma pergunta pra você.")
        self.assertEqual(self.say("Notification", {"notification_type": "elicitation_dialog"}),
                         "my app has a question for you.")

    def test_both_languages_have_every_phrase(self):
        self.assertEqual(set(self.vn.PHRASES["en"]), set(self.vn.PHRASES["pt-BR"]))
        self.assertEqual(set(self.vn.INSTRUCTION), set(self.vn.LANGUAGES))
        for text in self.vn.INSTRUCTION.values():
            self.assertIn("🔊", text)

    def test_instruction_asks_for_first_person_and_not_to_hide_the_project(self):
        en, pt = self.vn.INSTRUCTION["en"], self.vn.INSTRUCTION["pt-BR"]
        self.assertIn("first person", en)
        self.assertIn("this is <Name>", en)
        self.assertIn("which project", en)
        self.assertNotIn("don't repeat the project name", en)
        self.assertIn("primeira pessoa", pt)
        self.assertIn("aqui é o <Nome>", pt)
        self.assertIn("em que projeto", pt)
        self.assertNotIn("sem repetir o nome do projeto", pt)


class AgentNameTest(unittest.TestCase):
    def setUp(self):
        import voice_notify
        self.vn = voice_notify

    def test_portuguese_with_article(self):
        f = self.vn.agent_from_summary
        self.assertEqual(f("Alex, aqui é o Orion. Terminei a landing."), ("o", "Orion"))
        self.assertEqual(f("Aqui é a Luna, tenho uma dúvida."), ("a", "Luna"))
        self.assertEqual(f("Alex, aqui é a Kryden Web. Pronto."), ("a", "Kryden Web"))

    def test_english(self):
        f = self.vn.agent_from_summary
        self.assertEqual(f("Alex, this is Orion. I finished the landing page."), ("", "Orion"))
        self.assertEqual(f("This is Luna, I have a question."), ("", "Luna"))

    def test_no_name_when_the_agent_did_not_introduce_itself(self):
        f = self.vn.agent_from_summary
        self.assertIsNone(f("Alex, terminei o trabalho no acme web. Falta você aprovar."))
        self.assertIsNone(f("Alex, I finished the work on acme web."))
        self.assertIsNone(f("Terminei no kryden web, aqui é o resultado dos testes."))
        self.assertIsNone(f("Hi, this is done and tested."))
        self.assertIsNone(f(""))
        self.assertIsNone(f(None))

    def test_only_the_opening_counts(self):
        late = "Terminei a landing e subi tudo para o servidor de testes sem erro nenhum. " * 3 + "Aqui é o Orion."
        self.assertIsNone(self.vn.agent_from_summary(late))

    def test_label_keeps_the_article(self):
        self.assertEqual(self.vn.agent_label(("o", "Orion")), "O Orion")
        self.assertEqual(self.vn.agent_label(("a", "Luna")), "A Luna")
        self.assertEqual(self.vn.agent_label(("", "Orion")), "Orion")
        self.assertEqual(self.vn.agent_label(None), "")


@unittest.skipUnless(hasattr(os, "fork"), "flock queue needs macOS or Linux")
class QueueTest(StateTestCase):
    def test_fifo_order(self):
        for text in ("one", "two", "three"):
            self.vn.enqueue(text, "en", spawn=False)
        spoken = []
        with mock.patch.object(self.vn, "chosen_voice", return_value="v"):
            self.vn.speak_queue(lambda text, voice, lang: spoken.append((text, voice, lang)))
        self.assertEqual(spoken, [("one", "v", "en"), ("two", "v", "en"), ("three", "v", "en")])
        self.assertEqual(self.vn.QUEUE.read_text(), "")

    def test_broken_line_is_skipped(self):
        self.vn.enqueue("one", "en", spawn=False)
        with open(self.vn.QUEUE, "a") as f:
            f.write("not json\n")
        self.vn.enqueue("two", "en", spawn=False)
        self.assertEqual([self.vn.dequeue()["text"], self.vn.dequeue()["text"]], ["one", "two"])
        self.assertIsNone(self.vn.dequeue())

    def test_second_speaker_leaves_the_queue_to_the_first(self):
        self.vn.enqueue("one", "en", spawn=False)
        held = self.vn.lock("speaker.lock")
        try:
            spoken = []
            self.vn.speak_queue(lambda *a: spoken.append(a))
            self.assertEqual(spoken, [])
        finally:
            held.release()
        self.assertEqual(self.vn.dequeue()["text"], "one")


class HookTest(StateTestCase):
    def run_hook(self, payload, mac=True):
        with mock.patch.object(self.vn, "system_engine", return_value="avspeech" if mac else ""), \
                mock.patch.object(self.vn, "enqueue") as enqueue, \
                mock.patch.object(self.vn, "system_language", return_value="en-US"):
            out = self.vn.hook(json.dumps(payload))
        return out, enqueue

    def turn_on(self):
        Path(self.home).mkdir(parents=True, exist_ok=True)
        self.vn.ENABLED.write_text("")

    def test_user_prompt_submit_injects_the_instruction(self):
        self.turn_on()
        out, _ = self.run_hook({"hook_event_name": "UserPromptSubmit", "prompt": "hi"})
        data = json.loads(out)
        self.assertEqual(data["hookSpecificOutput"]["hookEventName"], "UserPromptSubmit")
        self.assertEqual(data["hookSpecificOutput"]["additionalContext"], self.vn.INSTRUCTION["en"])

    def test_language_from_config(self):
        self.turn_on()
        self.vn.save_config(language="pt-BR")
        out, _ = self.run_hook({"hook_event_name": "UserPromptSubmit"})
        self.assertEqual(json.loads(out)["hookSpecificOutput"]["additionalContext"], self.vn.INSTRUCTION["pt-BR"])

    def test_off_means_silent_and_no_instruction(self):
        out, enqueue = self.run_hook({"hook_event_name": "UserPromptSubmit"})
        self.assertEqual(out, "")
        out, enqueue = self.run_hook({"hook_event_name": "Stop", "cwd": self.home})
        enqueue.assert_not_called()

    def test_no_engine_means_no_instruction(self):
        self.turn_on()
        out, enqueue = self.run_hook({"hook_event_name": "UserPromptSubmit"}, mac=False)
        self.assertEqual(out, "")
        out, enqueue = self.run_hook({"hook_event_name": "Stop", "cwd": self.home}, mac=False)
        enqueue.assert_not_called()

    def test_own_program_is_enough_to_speak(self):
        self.turn_on()
        self.vn.save_config(command="my-tts")
        out, _ = self.run_hook({"hook_event_name": "UserPromptSubmit"}, mac=False)
        self.assertIn("additionalContext", out)

    def test_output_is_ascii_json(self):
        self.turn_on()
        out, _ = self.run_hook({"hook_event_name": "UserPromptSubmit"})
        out.encode("ascii")
        self.assertIn("🔊", json.loads(out)["hookSpecificOutput"]["additionalContext"])

    def test_stop_hook_active_and_sdk_are_skipped(self):
        self.turn_on()
        _, enqueue = self.run_hook({"hook_event_name": "Stop", "stop_hook_active": True, "cwd": self.home})
        enqueue.assert_not_called()
        with mock.patch.dict(os.environ, {"CLAUDE_CODE_ENTRYPOINT": "sdk-cli"}):
            out, enqueue = self.run_hook({"hook_event_name": "UserPromptSubmit"})
        self.assertEqual(out, "")

    def test_stop_queues_and_logs(self):
        self.turn_on()
        folder = Path(self.home) / "my-cool_app"
        folder.mkdir()
        with mock.patch.dict(os.environ, {"CLAUDE_PROJECT_DIR": str(folder)}):
            out, enqueue = self.run_hook({"hook_event_name": "Stop", "cwd": "/somewhere/else",
                                          "last_assistant_message": "ok\n🔊 All green."})
        self.assertEqual(out, "")
        text, lang = enqueue.call_args[0]
        self.assertEqual(text, "All green.")
        self.assertEqual(lang, "en")
        self.assertIn("Stop", self.vn.LOG.read_text())

    def folder(self, name="my-cool_app"):
        folder = Path(self.home) / name
        folder.mkdir(exist_ok=True)
        return folder

    def stop_then_ask(self, summary, lang, session="s1", other="s1"):
        """A Stop with the agent's line, then a permission prompt (in session `other`). Returns what was queued."""
        self.turn_on()
        self.vn.save_config(language=lang)
        with mock.patch.dict(os.environ, {"CLAUDE_PROJECT_DIR": str(self.folder())}):
            _, first = self.run_hook({"hook_event_name": "Stop", "session_id": session,
                                      "last_assistant_message": summary})
            _, second = self.run_hook({"hook_event_name": "Notification", "session_id": other,
                                       "notification_type": "permission_prompt"})
        return first.call_args[0][0], second.call_args[0][0]

    def test_name_from_the_summary_is_reused_for_the_permission_prompt_pt(self):
        spoken, ask = self.stop_then_ask("ok\n🔊 Alex, aqui é o Orion. Terminei a landing.", "pt-BR")
        self.assertEqual(spoken, "Alex, aqui é o Orion. Terminei a landing.")
        self.assertIn(ask, ["O Orion precisa de você.", "O Orion está esperando sua aprovação."])
        self.assertNotIn("my cool app", ask)

    def test_name_from_the_summary_is_reused_for_the_permission_prompt_en(self):
        spoken, ask = self.stop_then_ask("ok\n🔊 Alex, this is Luna. I finished the landing page.", "en")
        self.assertEqual(spoken, "Alex, this is Luna. I finished the landing page.")
        self.assertIn(ask, ["Luna needs you.", "Luna is waiting for your approval."])

    def test_feminine_article_is_kept(self):
        _, ask = self.stop_then_ask("🔊 Aqui é a Luna. Tenho uma dúvida.", "pt-BR")
        self.assertIn(ask, ["A Luna precisa de você.", "A Luna está esperando sua aprovação."])

    def test_no_name_stored_falls_back_to_the_repository(self):
        spoken, ask = self.stop_then_ask("🔊 Alex, terminei o trabalho no acme web.", "pt-BR")
        self.assertEqual(spoken, "Alex, terminei o trabalho no acme web.")
        self.assertIn(ask, ["my cool app precisa de você.", "my cool app está esperando sua aprovação."])
        self.assertFalse(self.vn.AGENTS.exists())

    def test_names_are_kept_per_session(self):
        _, ask = self.stop_then_ask("🔊 Aqui é o Orion. Pronto.", "pt-BR", session="s1", other="s2")
        self.assertIn(ask, ["my cool app precisa de você.", "my cool app está esperando sua aprovação."])
        with mock.patch.dict(os.environ, {"CLAUDE_PROJECT_DIR": str(self.folder())}):
            _, again = self.run_hook({"hook_event_name": "Notification", "session_id": "s1",
                                      "notification_type": "elicitation_dialog"})
        self.assertEqual(again.call_args[0][0], "O Orion tem uma pergunta pra você.")

    def test_stop_without_the_line_uses_the_remembered_name(self):
        self.turn_on()
        self.vn.save_config(language="pt-BR")
        self.vn.remember_agent("s1", ("a", "Luna"))
        with mock.patch.dict(os.environ, {"CLAUDE_PROJECT_DIR": str(self.folder())}):
            _, enqueue = self.run_hook({"hook_event_name": "Stop", "session_id": "s1",
                                        "last_assistant_message": "Done."})
        self.assertIn(enqueue.call_args[0][0], ["A Luna terminou.", "Pronto, A Luna acabou.",
                                                "A Luna terminou, dá uma olhada quando puder."])

    def test_a_new_name_replaces_the_old_one(self):
        self.vn.remember_agent("s1", ("o", "Orion"))
        self.vn.remember_agent("s1", ("a", "Luna"))
        self.assertEqual(self.vn.recall_agent("s1"), ("a", "Luna"))

    def test_nothing_is_stored_without_a_session_id(self):
        self.vn.remember_agent(None, ("o", "Orion"))
        self.assertFalse(self.vn.AGENTS.exists())
        self.assertIsNone(self.vn.recall_agent(None))
        self.assertIsNone(self.vn.recall_agent("never-seen"))

    def test_old_names_are_pruned(self):
        for i in range(self.vn.AGENTS_KEEP + 5):
            self.vn.remember_agent(f"s{i}", ("", f"Name{i}"))
        data = json.loads(self.vn.AGENTS.read_text())
        self.assertEqual(len(data), self.vn.AGENTS_KEEP)
        self.assertIn(f"s{self.vn.AGENTS_KEEP + 4}", data)
        self.assertNotIn("s0", data)

    def test_broken_state_file_is_ignored(self):
        self.vn.AGENTS.parent.mkdir(parents=True, exist_ok=True)
        self.vn.AGENTS.write_text("{not json")
        self.assertIsNone(self.vn.recall_agent("s1"))
        self.vn.remember_agent("s1", ("o", "Orion"))
        self.assertEqual(self.vn.recall_agent("s1"), ("o", "Orion"))

    def test_bad_input_never_raises(self):
        self.turn_on()
        with mock.patch.object(self.vn, "system_engine", return_value="avspeech"):
            self.assertEqual(self.vn.hook("{not json"), "")

    def test_main_prints_only_for_user_prompt_submit(self):
        self.turn_on()
        buf = io.StringIO()
        with mock.patch.object(self.vn, "system_engine", return_value="avspeech"), \
                mock.patch.object(self.vn, "system_language", return_value="en"), \
                mock.patch.object(sys, "stdin", io.TextIOWrapper(io.BytesIO('{"hook_event_name": "UserPromptSubmit"}'.encode()))), \
                redirect_stdout(buf):
            self.vn.main(["hook"])
        self.assertIn("additionalContext", json.loads(buf.getvalue())["hookSpecificOutput"])


class LanguageAndVoiceTest(StateTestCase):
    def test_language(self):
        self.assertEqual(self.vn.language({"language": "pt-BR"}), "pt-BR")
        with mock.patch.object(self.vn, "system_language", return_value="pt-PT"):
            self.assertEqual(self.vn.language({}), "pt-BR")
        with mock.patch.object(self.vn, "system_language", return_value="de-DE"):
            self.assertEqual(self.vn.language({"language": "auto"}), "en")

    def test_apple_languages_parsing(self):
        out = subprocess.CompletedProcess([], 0, '(\n    "pt-BR",\n    "en-US"\n)\n', "")
        with mock.patch.object(self.vn, "system", return_value="mac"), mock.patch.object(self.vn, "run", return_value=out):
            self.assertEqual(self.vn.system_language(), "pt-BR")
        out = subprocess.CompletedProcess([], 0, "(\n    en,\n    fr\n)\n", "")
        with mock.patch.object(self.vn, "system", return_value="mac"), mock.patch.object(self.vn, "run", return_value=out):
            self.assertEqual(self.vn.system_language(), "en")

    def test_best_voice(self):
        voices = [
            ("com.apple.voice.compact.pt-BR.Luciana", "Luciana", "pt-BR", 1),
            ("com.apple.ttsbundle.premium_pt-BR", "Premium", "pt-BR", 2),
            ("com.apple.siri.natural.Voice", "Voice 2", "pt-BR", 2),
            ("com.apple.voice.premium.pt-PT.Joana", "Joana", "pt-PT", 3),
            ("com.apple.speech.synthesis.voice.Bells", "Bells", "en-US", 1),
            ("com.apple.eloquence.en-US.Eddy", "Eddy", "en-US", 1),
            ("com.apple.voice.super-compact.en-GB.Daniel", "Daniel", "en-GB", 1),
            ("com.apple.voice.compact.en-US.Samantha", "Samantha", "en-US", 1),
        ]
        self.assertEqual(self.vn.best_voice("pt-BR", voices)[0], "com.apple.siri.natural.Voice")
        self.assertEqual(self.vn.best_voice("en", voices)[0], "com.apple.voice.compact.en-US.Samantha")
        self.assertIsNone(self.vn.best_voice("pt-BR", voices[3:]))

    def test_configured_voice_wins(self):
        with mock.patch.object(self.vn, "installed_voices") as voices:
            self.assertEqual(self.vn.chosen_voice("en", {"voice": "Samantha"}), "Samantha")
            voices.assert_not_called()


class EngineTest(StateTestCase):
    def speak(self, platform, engine, voice="", command="", rc=0):
        calls = []

        def fake_run(cmd, **kw):
            calls.append((cmd, kw))
            return subprocess.CompletedProcess(cmd, rc, "", "")
        with mock.patch.object(self.vn, "system", return_value=platform), \
                mock.patch.object(self.vn, "system_engine", return_value=engine), \
                mock.patch.object(self.vn, "run", side_effect=fake_run):
            self.vn.speak("hello there", voice, "pt-BR", command)
        return calls

    def test_engine_per_system(self):
        with mock.patch.object(self.vn, "system", return_value="mac"):
            self.assertEqual(self.vn.system_engine(), "avspeech")
        with mock.patch.object(self.vn, "system", return_value="linux"), \
                mock.patch.object(self.vn.shutil, "which", side_effect=lambda e: e == "espeak-ng" and "/usr/bin/x"):
            self.assertEqual(self.vn.system_engine(), "espeak-ng")
        with mock.patch.object(self.vn, "system", return_value="windows"), \
                mock.patch.object(self.vn.shutil, "which", return_value=None):
            self.assertEqual(self.vn.system_engine(), "")
            self.assertFalse(self.vn.can_speak({}))
            self.assertTrue(self.vn.can_speak({"command": "my-tts"}))

    def test_mac(self):
        cmd = self.speak("mac", "avspeech", "com.apple.siri.natural.Sandra")[0][0]
        self.assertEqual(cmd[:2], ["osascript", "-l"])
        self.assertEqual(cmd[-3:], ["com.apple.siri.natural.Sandra", "pt-BR", "hello there"])
        self.assertEqual(self.speak("mac", "avspeech", "Luciana")[0][0], ["say", "-v", "Luciana", "hello there"])
        calls = self.speak("mac", "avspeech", "com.apple.x", rc=1)
        self.assertEqual(calls[-1][0], ["say", "hello there"])

    def test_linux(self):
        self.assertEqual(self.speak("linux", "spd-say", "")[0][0], ["spd-say", "-w", "-l", "pt-BR", "hello there"])
        self.assertEqual(self.speak("linux", "spd-say", "female1")[0][0][-3:], ["-y", "female1", "hello there"])
        self.assertEqual(self.speak("linux", "espeak-ng", "")[0][0], ["espeak-ng", "-v", "pt-br", "hello there"])
        with mock.patch.object(self.vn.shutil, "which", return_value="/usr/bin/espeak-ng"):
            calls = self.speak("linux", "spd-say", "female1", rc=1)
        self.assertEqual(calls[-1][0], ["espeak-ng", "-v", "pt-br", "hello there"])

    def test_windows_passes_text_in_the_environment(self):
        cmd, kw = self.speak("windows", "sapi", "Microsoft Maria Desktop")[0]
        self.assertEqual(cmd[0], "powershell")
        self.assertNotIn("hello there", " ".join(cmd))
        self.assertEqual(kw["env"]["VOICE_NOTIFY_TEXT"], "hello there")
        self.assertEqual(kw["env"]["VOICE_NOTIFY_VOICE"], "Microsoft Maria Desktop")

    def test_windows_language(self):
        with mock.patch.object(self.vn, "system", return_value="windows"), \
                mock.patch.object(self.vn, "windows_ui_language", return_value="pt_BR"):
            self.assertEqual(self.vn.language({}), "pt-BR")

    def test_own_program_first_then_fallback(self):
        with mock.patch.object(self.vn, "run_command", return_value=True) as own:
            self.assertEqual(self.speak("linux", "espeak-ng", command="my-tts"), [])
        own.assert_called_once()
        with mock.patch.object(self.vn, "run_command", return_value=False):
            self.assertEqual(self.speak("linux", "espeak-ng", command="my-tts")[0][0][0], "espeak-ng")

    @unittest.skipIf(os.name == "nt", "POSIX shell")
    def test_own_program_gets_the_text_safely(self):
        out = Path(self.home) / "said.txt"
        text = 'it\'s "done"; $(touch pwned) `x`'
        with mock.patch.object(self.vn, "system", return_value="linux"):
            ok = self.vn.run_command(f"printf '%s|%s' \"$VOICE_NOTIFY_LANG\" > {shlex.quote(str(out))}", text, "", "en")
        self.assertTrue(ok)
        self.assertEqual(out.read_text(), "en|" + text)
        self.assertFalse((Path(self.home) / "pwned").exists())
        with mock.patch.object(self.vn, "system", return_value="linux"):
            self.assertFalse(self.vn.run_command("exit 3", "x", "", "en"))


@unittest.skipIf(os.name == "nt", "POSIX shell")
class LauncherTest(StateTestCase):
    def test_run_sh_finds_python(self):
        env = dict(os.environ, CLAUDE_VOICE_NOTIFY_HOME=self.home)
        r = subprocess.run(["sh", str(ROOT / "scripts" / "run.sh"), "status"], env=env, capture_output=True, text=True)
        self.assertIn("voice notifications: off", r.stdout)
        r = subprocess.run(["sh", str(ROOT / "scripts" / "run.sh"), "hook"], input='{"hook_event_name": "Stop"}',
                           env=env, capture_output=True, text=True)
        self.assertEqual((r.returncode, r.stdout), (0, ""))


@unittest.skipUnless(shutil.which("git"), "needs git")
class SpokenNameTest(StateTestCase):
    def git(self, *args, cwd):
        subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)

    def test_repo_name_and_worktree(self):
        repo = Path(self.home) / "acme-web_app"
        repo.mkdir()
        self.git("init", "-q", cwd=repo)
        self.git("-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-q", "--allow-empty", "-m", "x",
                 cwd=repo)
        (repo / "sub").mkdir()
        self.assertEqual(self.vn.spoken_name(str(repo / "sub")), "acme web app")
        worktree = Path(self.home) / "wt-123"
        self.git("worktree", "add", "-q", str(worktree), cwd=repo)
        self.assertEqual(self.vn.spoken_name(str(worktree)), "acme web app")

    def test_plain_folder(self):
        folder = Path(self.home) / "notes.v2"
        folder.mkdir()
        self.assertEqual(self.vn.spoken_name(str(folder)), "notes v2")


class CommandTest(StateTestCase):
    def test_no_engine_explains_and_stays_quiet(self):
        with mock.patch.object(self.vn, "system", return_value="linux"), \
                mock.patch.object(self.vn.shutil, "which", return_value=None), \
                mock.patch.object(self.vn, "enqueue") as enqueue:
            self.assertIn("No speech engine found", self.vn.command(["on"]))
            self.assertIn("No speech engine found", self.vn.command(["test"]))
        enqueue.assert_not_called()

    def test_command_and_voice_with_spaces(self):
        with mock.patch.object(self.vn, "system", return_value="linux"), \
                mock.patch.object(self.vn, "system_engine", return_value="espeak-ng"):
            self.vn.command(["command", "say", "-r", "230"])
            self.assertEqual(self.vn.load_config()["command"], "say -r 230")
            self.vn.command(["command", "my tts", "--voice", "Ana Paula"])
            self.assertEqual(self.vn.load_config()["command"], "'my tts' --voice 'Ana Paula'")
            self.assertIn("your program", self.vn.command(["status"]))
            self.vn.command(["command", "off"])
            self.assertEqual(self.vn.load_config()["command"], "")
            self.vn.command(["voice", "Microsoft", "Maria", "Desktop"])
            self.assertEqual(self.vn.load_config()["voice"], "Microsoft Maria Desktop")
        with mock.patch.object(self.vn, "system", return_value="windows"):
            self.assertEqual(self.vn.join_args(["C:/tools/my tts.exe", "-v", "x"]), '"C:/tools/my tts.exe" -v x')

    def test_test_speaks_a_first_person_sample_without_the_repository(self):
        with mock.patch.object(self.vn, "system_engine", return_value="avspeech"), \
                mock.patch.object(self.vn, "enqueue") as enqueue, \
                mock.patch.object(self.vn, "system_language", return_value="en"):
            self.vn.command(["test"])
        self.assertEqual(enqueue.call_args[0], (self.vn.PHRASES["en"]["test"], "en"))

    def test_on_off_lang_voice(self):
        with mock.patch.object(self.vn, "system_engine", return_value="avspeech"), \
                mock.patch.object(self.vn, "enqueue") as enqueue, \
                mock.patch.object(self.vn, "installed_voices", return_value=[]), \
                mock.patch.object(self.vn, "system_language", return_value="en"):
            self.vn.command(["on", "Samantha"])
            self.assertTrue(self.vn.ENABLED.exists())
            self.assertEqual(enqueue.call_args[0], ("Voice on.", "en"))
            self.assertEqual(self.vn.load_config()["voice"], "Samantha")
            self.vn.command(["lang", "pt-BR"])
            self.assertIn("language: pt-BR", self.vn.command(["status"]))
            self.vn.command(["voice", "auto"])
            self.assertEqual(self.vn.load_config()["voice"], "")
            self.assertIn("usage", self.vn.command(["lang", "fr"]))
            self.assertIn("off", self.vn.command(["off"]))
            self.assertFalse(self.vn.ENABLED.exists())


if __name__ == "__main__":
    unittest.main()
