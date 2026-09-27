"""
Offline resolution of a batch of schemas.

This module splits reference *resolution* (and its caching) out from
*validation*. Given a batch of schemas -- e.g. the ones a service
preloads at startup -- `resolve` eagerly:

* builds and crawls a `referencing` registry containing the batch,
* finds every ``$ref`` (and notes every ``$dynamicRef`` /
  ``$recursiveRef``) along with where it appears,
* resolves each reference, recording the cache key under which its
  target is stored and which resource / fragment it lands on,
* detects ``$ref`` cycles, which at validation time are *not* cut off
  by the validator -- they recurse until `RecursionError` (or, when
  the instance never reaches the cyclic subschema, silently pass).

The result is a `ResolutionPlan`. Validators consume only the plan's
(crawled, immutable) registry -- no retrieval or discovery happens at
validation time unless a reference was already reported in the plan's
`ResolutionPlan.errors`.
"""
from __future__ import annotations

from collections.abc import Mapping
from urllib.parse import urldefrag, urljoin

from attrs import field, frozen
import referencing.exceptions
import referencing.jsonschema

#: Keywords whose values are instance data, not subschemas. The walk
#: below does not descend into them, so a ``"$ref"`` key appearing in
#: e.g. an ``enum`` value is not mistaken for a reference.
_NON_SCHEMA_KEYWORDS = frozenset({"const", "default", "enum", "examples"})

#: Reference keywords whose target depends on the runtime dynamic scope
#: and therefore cannot be resolved statically.
_DYNAMIC_KEYWORDS = frozenset({"$dynamicRef", "$recursiveRef"})

__all__ = ["RefResolution", "ResolutionPlan", "resolve"]


@frozen
class RefResolution:
    """
    Where a single reference in a schema batch lands.

    ``cache_key`` is the key under which the target resource is stored
    in (and retrieved from) the registry's cache: the reference resolved
    against its base URI and defragmented, i.e. precisely
    ``urldefrag(urljoin(base_uri, ref))[0]`` -- matching
    `referencing.Resolver.lookup`. For plain-name fragments (anchors)
    the registry additionally caches under ``(cache_key, fragment)``.
    """

    #: URI identifying the schema document containing the reference.
    source: str
    #: Path of the reference keyword within its schema document.
    location: tuple = field(repr=lambda v: "/".join(map(str, v)))
    #: The raw value of the reference keyword.
    ref: str = field(repr=True)
    #: The base URI the reference was resolved against.
    base_uri: str = ""
    #: The registry cache key of the target resource (see above).
    cache_key: str | None = None
    #: The fragment: a JSON pointer, an anchor name, or empty.
    fragment: str = ""
    #: Whether this is a ``$dynamicRef`` / ``$recursiveRef``, whose
    #: target depends on the runtime dynamic scope and is therefore
    #: not resolved here.
    dynamic: bool = False
    #: Why resolution failed, if it did.
    error: str | None = None
    #: The chain of references -- starting with this one -- which loops
    #: back to a schema already on the chain, if a loop was detected.
    cycle: tuple | None = None

    @property
    def ok(self) -> bool:
        """
        Whether the reference resolved cleanly, without cycles.
        """
        return self.error is None and self.cycle is None


@frozen
class ResolutionPlan:
    """
    The result of resolving a batch of schemas offline.
    """

    #: The crawled registry containing every resource in the batch.
    registry: referencing.jsonschema.SchemaRegistry
    #: One entry per reference found in the batch.
    references: tuple[RefResolution, ...]

    @property
    def errors(self) -> list[RefResolution]:
        """
        All references which failed to resolve or form a cycle.
        """
        return [each for each in self.references if not each.ok]

    @property
    def cache_keys(self) -> list[str]:
        """
        The cache keys (canonical URIs) of all known resources.
        """
        return sorted(self.registry)

    def validator(self, schema, Validator=None, **kwargs):
        """
        Create a validator which consumes only this plan's registry.
        """
        from jsonschema.validators import validator_for

        if Validator is None:
            Validator = validator_for(schema)
        return Validator(schema, registry=self.registry, **kwargs)


def resolve(
    schemas,
    specification=referencing.jsonschema.DRAFT202012,
    retrieve=None,
) -> ResolutionPlan:
    """
    Resolve a batch of schemas offline, returning a `ResolutionPlan`.

    Arguments:

        schemas:

            either a mapping of URI to schema, or an iterable of
            schemas (which are then identified by their ``$id``, or
            by generated URIs when they have none)

        specification:

            the `referencing.jsonschema.Specification` to interpret
            schemas under when they do not say so themselves via
            ``$schema``

        retrieve:

            an optional retrieval callback for references to resources
            outside the batch; by default nothing is retrieved, so any
            such reference shows up in `ResolutionPlan.errors`
    """
    if isinstance(schemas, Mapping):
        pairs = list(schemas.items())
    else:
        pairs = [
            (
                schema.get("$id") or f"urn:jsonschema:offline:{index}"
                if isinstance(schema, Mapping)
                else f"urn:jsonschema:offline:{index}",
                schema,
            )
            for index, schema in enumerate(schemas)
        ]

    if retrieve is None:
        registry = referencing.jsonschema.SchemaRegistry()
    else:
        registry = referencing.jsonschema.SchemaRegistry(retrieve=retrieve)
    registry = registry.with_contents(
        pairs, default_specification=specification,
    ).crawl()

    references = []
    for uri, schema in pairs:
        resource = registry[uri]
        base_uri = resource.id() or uri
        resolver = registry.resolver(base_uri=base_uri)
        _walk(specification, uri, schema, resolver, base_uri, (), references)
    return ResolutionPlan(registry=registry, references=tuple(references))


def _walk(specification, source, node, resolver, base_uri, location, out):
    if isinstance(node, Mapping):
        id = specification.id_of(node)
        if id is not None:
            base_uri = urljoin(base_uri, id.rstrip("#"))
            resolver = resolver.in_subresource(
                specification.create_resource(node),
            )

        ref = node.get("$ref")
        if isinstance(ref, str):
            out.append(
                _resolve_one(source, location + ("$ref",), ref, resolver),
            )
        for keyword in _DYNAMIC_KEYWORDS:
            dynamic_ref = node.get(keyword)
            if isinstance(dynamic_ref, str):
                out.append(
                    RefResolution(
                        source=source,
                        location=location + (keyword,),
                        ref=dynamic_ref,
                        base_uri=base_uri,
                        dynamic=True,
                    ),
                )

        for keyword, value in node.items():
            if keyword not in _NON_SCHEMA_KEYWORDS:
                _walk(
                    specification,
                    source,
                    value,
                    resolver,
                    base_uri,
                    location + (keyword,),
                    out,
                )
    elif isinstance(node, list):
        for index, value in enumerate(node):
            _walk(
                specification,
                source,
                value,
                resolver,
                base_uri,
                location + (index,),
                out,
            )


def _resolve_one(source, location, ref, resolver):
    base_uri = resolver._base_uri
    if ref.startswith("#"):
        uri, fragment = base_uri, ref[1:]
    else:
        uri, fragment = urldefrag(urljoin(base_uri, ref))

    try:
        resolved = resolver.lookup(ref)
    except referencing.exceptions.Unresolvable as error:
        return RefResolution(
            source=source,
            location=location,
            ref=ref,
            base_uri=base_uri,
            cache_key=uri,
            fragment=fragment,
            error=str(error),
        )
    cycle = _detect_cycle(resolved)
    return RefResolution(
        source=source,
        location=location,
        ref=ref,
        base_uri=base_uri,
        cache_key=uri,
        fragment=fragment,
        cycle=None if cycle is None else (ref,) + cycle,
    )


def _detect_cycle(resolved):
    """
    Follow a chain of ``$ref`` keywords exactly as validation would.

    Validators evaluate ``$ref`` by descending into the resolved
    contents, where a further ``$ref`` keyword triggers again -- so a
    chain of references which returns to a schema already on the chain
    never terminates at runtime. Returns the chain of references from
    the first repeated schema onwards, or `None` if the chain ends.

    Unresolvable references further down the chain are reported when
    the walk visits them directly, so they simply end the chain here.
    """
    contents, resolver = resolved.contents, resolved.resolver
    seen, chain = set(), []
    while isinstance(contents, Mapping):
        ref = contents.get("$ref")
        if not isinstance(ref, str):
            return None
        if id(contents) in seen:
            return tuple(chain)
        seen.add(id(contents))
        chain.append(ref)
        try:
            resolved = resolver.lookup(ref)
        except referencing.exceptions.Unresolvable:
            return None
        contents, resolver = resolved.contents, resolved.resolver
    return None
