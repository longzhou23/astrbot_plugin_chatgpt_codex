from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

from .. import agent_provider as adapter


class ProviderLifecycleTests(TestCase):
    def setUp(self):
        self.current = type("CodexProvider", (), {"__module__": adapter.__name__})
        self.foreign = type("OtherProvider", (), {"__module__": "other_plugin.adapter"})
        self.owned = SimpleNamespace(type="chatgpt_codex", cls_type=self.current)
        self.other = SimpleNamespace(type="other", cls_type=self.foreign)
        self.registry = [self.other, self.owned]
        self.mapping = {"chatgpt_codex": self.owned, "other": self.other}
        self.patcher = patch.multiple(
            adapter, create=True, _ASTRBOT_AVAILABLE=True,
            provider_registry=self.registry, provider_cls_map=self.mapping,
            CodexProvider=self.current, _SERVICE=None,
        )
        self.patcher.start()
        self.addCleanup(self.patcher.stop)

    def test_uninstall_clears_only_own_registration_and_service(self):
        service = object()
        adapter.bind_service(service)
        adapter.release_service(service)
        self.assertNotIn("chatgpt_codex", self.mapping)
        self.assertEqual(self.registry, [self.other])
        self.assertIsNone(adapter._SERVICE)
        # Repeated unload is harmless.
        adapter.release_service(service)
        self.assertEqual(self.mapping, {"other": self.other})

    def test_failed_load_leftovers_are_removed_before_registration(self):
        self.registry.append(self.owned)
        adapter.unregister_provider_adapter()
        self.assertEqual(self.registry, [self.other])
        self.assertNotIn("chatgpt_codex", self.mapping)

    def test_other_plugin_name_collision_is_preserved(self):
        collision = SimpleNamespace(type="chatgpt_codex", cls_type=self.foreign)
        self.mapping["chatgpt_codex"] = collision
        self.registry.append(collision)
        adapter.unregister_provider_adapter()
        self.assertIs(self.mapping["chatgpt_codex"], collision)
        self.assertIn(collision, self.registry)

    def test_old_instance_cannot_unregister_new_instance(self):
        old, new = object(), object()
        adapter.bind_service(old)
        adapter.bind_service(new)
        adapter.release_service(old)
        self.assertIs(adapter._SERVICE, new)
        self.assertIs(self.mapping["chatgpt_codex"], self.owned)

    def test_old_class_cleanup_preserves_replacement_class(self):
        old_class = type("CodexProvider", (), {"__module__": adapter.__name__})
        adapter.unregister_provider_adapter(old_class)
        self.assertIs(self.mapping["chatgpt_codex"], self.owned)

    def test_legacy_backup_registration_is_reclaimed(self):
        backup = type("CodexProvider", (), {
            "__module__": "data.plugins.backup-astrbot_plugin_chatgpt_codex-67719e0.agent_provider",
        })
        stale = SimpleNamespace(type="chatgpt_codex", cls_type=backup)
        self.mapping["chatgpt_codex"] = stale
        self.registry.append(stale)
        adapter.unregister_provider_adapter()
        self.assertEqual(self.registry, [self.other])
        self.assertNotIn("chatgpt_codex", self.mapping)

    def test_similarly_named_foreign_module_is_preserved(self):
        foreign = type("CodexProvider", (), {
            "__module__": "data.plugins.backup-astrbot_plugin_chatgpt_codex-foreign.agent_provider",
        })
        collision = SimpleNamespace(type="chatgpt_codex", cls_type=foreign)
        self.mapping["chatgpt_codex"] = collision
        self.registry.append(collision)
        adapter.unregister_provider_adapter()
        self.assertIs(self.mapping["chatgpt_codex"], collision)
