from unittest import TestCase

from referencing.jsonschema import DRAFT202012

from jsonschema import resolving


class TestResolve(TestCase):
    def test_finds_every_ref_and_where_it_lands(self):
        plan = resolving.resolve({
            "http://example.com/defs.json": {
                "$defs": {"name": {"type": "string"}},
            },
            "http://example.com/root.json": {
                "properties": {
                    "name": {"$ref": "defs.json#/$defs/name"},
                },
            },
        })

        self.assertEqual(plan.errors, [])
        (ref,) = plan.references
        self.assertEqual(ref.source, "http://example.com/root.json")
        self.assertEqual(ref.location, ("properties", "name", "$ref"))
        self.assertEqual(ref.base_uri, "http://example.com/root.json")
        self.assertEqual(ref.cache_key, "http://example.com/defs.json")
        self.assertEqual(ref.fragment, "/$defs/name")
        self.assertTrue(ref.ok)

    def test_cache_keys_are_canonical_uris_of_known_resources(self):
        plan = resolving.resolve({
            "http://example.com/a.json": {"type": "integer"},
            "http://example.com/b.json": {"$ref": "a.json"},
        })
        self.assertEqual(
            plan.cache_keys,
            ["http://example.com/a.json", "http://example.com/b.json"],
        )

    def test_unresolvable_refs_are_reported_not_raised(self):
        plan = resolving.resolve({
            "http://example.com/a.json": {"$ref": "missing.json"},
        })
        (error,) = plan.errors
        self.assertEqual(error.ref, "missing.json")
        self.assertEqual(error.cache_key, "http://example.com/missing.json")
        self.assertIsNotNone(error.error)

    def test_refs_in_instance_data_are_ignored(self):
        plan = resolving.resolve({
            "http://example.com/a.json": {
                "const": {"$ref": "not-a-reference"},
                "enum": [{"$ref": "also-not-a-reference"}],
            },
        })
        self.assertEqual(plan.references, ())

    def test_dynamic_refs_are_noted_but_not_resolved(self):
        plan = resolving.resolve({
            "http://example.com/a.json": {
                "$dynamicRef": "#anchor",
                "$defs": {"anchored": {"$anchor": "anchor"}},
            },
        })
        (ref,) = plan.references
        self.assertTrue(ref.dynamic)
        self.assertTrue(ref.ok)

    def test_cycles_are_detected_statically(self):
        plan = resolving.resolve({
            "http://example.com/a.json": {"$ref": "b.json"},
            "http://example.com/b.json": {"$ref": "a.json"},
        })
        cycles = [each.cycle for each in plan.errors]
        self.assertEqual(
            cycles,
            [("b.json", "a.json", "b.json"), ("a.json", "b.json", "a.json")],
        )

    def test_self_referential_cycle(self):
        plan = resolving.resolve({
            "http://example.com/loop.json": {"$ref": "#"},
        })
        (error,) = plan.errors
        self.assertEqual(error.cycle, ("#", "#"))

    def test_acyclic_chains_have_no_cycle(self):
        plan = resolving.resolve({
            "http://example.com/a.json": {"$ref": "b.json"},
            "http://example.com/b.json": {"type": "string"},
        })
        self.assertEqual(plan.errors, [])

    def test_iterable_of_schemas_without_ids(self):
        plan = resolving.resolve([{"type": "integer"}, True])
        self.assertEqual(plan.errors, [])
        self.assertEqual(
            plan.cache_keys,
            ["urn:jsonschema:offline:0", "urn:jsonschema:offline:1"],
        )

    def test_nested_ids_change_the_base_uri(self):
        plan = resolving.resolve({
            "http://example.com/root.json": {
                "$defs": {
                    "child": {
                        "$id": "child.json",
                        "$ref": "other.json",
                    },
                },
            },
        })
        (ref,) = plan.references
        self.assertEqual(ref.base_uri, "http://example.com/child.json")
        self.assertEqual(ref.cache_key, "http://example.com/other.json")


class TestResolutionPlanValidator(TestCase):
    def test_validator_consumes_only_the_plan(self):
        plan = resolving.resolve({
            "http://example.com/defs.json": {
                "$defs": {"name": {"type": "string"}},
            },
            "http://example.com/root.json": {
                "$id": "http://example.com/root.json",
                "properties": {
                    "name": {"$ref": "defs.json#/$defs/name"},
                },
            },
        })
        validator = plan.validator(
            {"$id": "http://example.com/root.json",
             "properties": {"name": {"$ref": "defs.json#/$defs/name"}}},
        )
        self.assertEqual(
            [list(error.path) for error in validator.iter_errors({"name": 3})],
            [["name"]],
        )
        self.assertEqual(list(validator.iter_errors({"name": "ok"})), [])

    def test_validator_uses_the_plan_registry_for_retrieval(self):
        def retrieve(uri):  # pragma: no cover
            self.fail("validation should not retrieve anything")

        plan = resolving.resolve(
            {"http://example.com/a.json": {"$ref": "b.json"},
             "http://example.com/b.json": {"type": "string"}},
        )
        validator = plan.validator(
            {"$id": "http://example.com/a.json", "$ref": "b.json"},
        )
        validator.validate("fine")
