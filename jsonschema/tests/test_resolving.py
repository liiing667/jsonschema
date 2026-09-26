from unittest import TestCase

from jsonschema import Draft202012Validator
from jsonschema.resolving import resolve


class TestResolve(TestCase):
    def test_lists_each_ref_with_location_and_cache_key(self):
        plan = resolve({
            "urn:example:defs": {
                "$id": "urn:example:defs",
                "$defs": {"positive": {"type": "integer", "minimum": 1}},
            },
            "urn:example:root": {
                "$id": "urn:example:root",
                "properties": {
                    "a": {"$ref": "urn:example:defs#/$defs/positive"},
                    "b": {
                        "items": {"$ref": "urn:example:defs#/$defs/positive"},
                    },
                },
            },
        })

        self.assertEqual(plan.errors, [])
        self.assertEqual(len(plan.references), 2)
        first, second = plan.references
        self.assertEqual(first.source, "urn:example:root")
        self.assertEqual(first.location, ("properties", "a", "$ref"))
        self.assertEqual(first.cache_key, "urn:example:defs")
        self.assertEqual(first.fragment, "/$defs/positive")
        self.assertEqual(second.location, ("properties", "b", "items", "$ref"))
        self.assertEqual(second.cache_key, "urn:example:defs")
        self.assertIn("urn:example:defs", plan.cache_keys)

    def test_relative_refs_resolve_against_enclosing_id(self):
        plan = resolve({
            "http://example.com/root": {
                "$id": "http://example.com/root",
                "$defs": {
                    "child": {
                        "$id": "child",
                        "$defs": {"leaf": {"type": "string"}},
                    },
                },
                "properties": {
                    "a": {"$ref": "child#/$defs/leaf"},
                },
            },
        })
        self.assertEqual(plan.errors, [])
        [ref] = plan.references
        self.assertEqual(ref.base_uri, "http://example.com/root")
        self.assertEqual(ref.cache_key, "http://example.com/child")

    def test_unresolvable_refs_are_reported_not_raised(self):
        plan = resolve([{"$ref": "urn:example:missing"}])
        [error] = plan.errors
        self.assertEqual(error.cache_key, "urn:example:missing")
        self.assertIn("urn:example:missing", error.error)

    def test_ref_cycles_are_detected(self):
        plan = resolve([{"$ref": "#"}])
        [error] = plan.errors
        self.assertEqual(error.cycle, ("#", "#"))

    def test_indirect_ref_cycles_are_detected(self):
        plan = resolve([{
            "$ref": "#/$defs/a",
            "$defs": {
                "a": {"$ref": "#/$defs/b"},
                "b": {"$ref": "#/$defs/a"},
            },
        }])
        root, *_ = plan.references
        self.assertEqual(
            root.cycle, ("#/$defs/a", "#/$defs/b", "#/$defs/a"),
        )
        self.assertEqual(len(plan.errors), 3)

    def test_recursive_schemas_through_applicators_are_not_cycles(self):
        plan = resolve([{
            "$id": "urn:example:tree",
            "type": "object",
            "properties": {"child": {"$ref": "#"}},
        }])
        self.assertEqual(plan.errors, [])
        self.assertEqual(len(plan.references), 1)

    def test_dynamic_refs_are_noted_but_not_statically_resolved(self):
        plan = resolve([{
            "$id": "urn:example:root",
            "$dynamicAnchor": "node",
            "properties": {"child": {"$dynamicRef": "#node"}},
        }])
        self.assertEqual(plan.errors, [])
        [ref] = plan.references
        self.assertTrue(ref.dynamic)
        self.assertEqual(ref.location, ("properties", "child", "$dynamicRef"))

    def test_refs_inside_instance_data_are_ignored(self):
        plan = resolve([{"enum": [{"$ref": "not-a-reference"}]}])
        self.assertEqual(plan.references, ())

    def test_validators_consume_the_plan(self):
        plan = resolve({
            "urn:example:defs": {
                "$id": "urn:example:defs",
                "$defs": {"positive": {"type": "integer", "minimum": 1}},
            },
            "urn:example:root": {
                "$id": "urn:example:root",
                "properties": {
                    "a": {"$ref": "urn:example:defs#/$defs/positive"},
                },
            },
        })
        validator = plan.validator({
            "$id": "urn:example:root",
            "properties": {
                "a": {"$ref": "urn:example:defs#/$defs/positive"},
            },
        })
        self.assertIsInstance(validator, Draft202012Validator)
        [error] = validator.iter_errors({"a": -1})
        self.assertEqual(list(error.path), ["a"])
        self.assertEqual(
            list(error.schema_path), ["properties", "a", "minimum"],
        )
        self.assertTrue(validator.is_valid({"a": 3}))

    def test_boolean_schemas(self):
        plan = resolve([True, False])
        self.assertEqual(plan.references, ())
        self.assertEqual(plan.errors, [])
