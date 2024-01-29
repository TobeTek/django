import functools
from abc import ABC, abstractmethod
from collections import namedtuple, defaultdict
from django.db.models import query_utils, signals, sql
from django.db import IntegrityError, connections, models, transaction


def make_model_tuple(model):
    """
    Take a model or a string of the form "app_label.ModelName" and return a
    corresponding ("app_label", "modelname") tuple. If a tuple is passed in,
    assume it's a valid model tuple already and return it unchanged.
    """
    try:
        if isinstance(model, tuple):
            model_tuple = model
        elif isinstance(model, str):
            app_label, model_name = model.split(".")
            model_tuple = app_label, model_name.lower()
        else:
            model_tuple = model._meta.app_label, model._meta.model_name
        assert len(model_tuple) == 2
        return model_tuple
    except (ValueError, AssertionError):
        raise ValueError(
            "Invalid model reference '%s'. String model references "
            "must be of the form 'app_label.ModelName'." % model
        )


def resolve_callables(mapping):
    """
    Generate key/value pairs for the given mapping where the values are
    evaluated if they're callable.
    """
    for k, v in mapping.items():
        yield k, v() if callable(v) else v


def unpickle_named_row(names, values):
    return create_namedtuple_class(*names)(*values)


@functools.lru_cache
def create_namedtuple_class(*names):
    # Cache type() with @lru_cache since it's too slow to be called for every
    # QuerySet evaluation.
    def __reduce__(self):
        return unpickle_named_row, (names, tuple(self))

    return type(
        "Row",
        (namedtuple("Row", names),),
        {"__reduce__": __reduce__, "__slots__": ()},
    )


class AltersData:
    """
    Make subclasses preserve the alters_data attribute on overridden methods.
    """

    def __init_subclass__(cls, **kwargs):
        for fn_name, fn in vars(cls).items():
            if callable(fn) and not hasattr(fn, "alters_data"):
                for base in cls.__bases__:
                    if base_fn := getattr(base, fn_name, None):
                        if hasattr(base_fn, "alters_data"):
                            fn.alters_data = base_fn.alters_data
                        break

        super().__init_subclass__(**kwargs)


class BaseCollector(ABC):
    def __init__(self, using, origin=None):
        self.using = using
        # A Model or QuerySet object.
        self.origin = origin
        # Initially, {model: {instances}}, later values become lists.
        self.data = defaultdict(set)
        # {(field, value): [instances, …]}
        self.field_updates = defaultdict(list)
        # {model: {field: {instances}}}
        self.restricted_objects = defaultdict(partial(defaultdict, set))

        # fast_mod_objs is a list of queryset-likes that can be modified (deleted/updated) without
        # fetching the objects into memory.
        self.fast_mod_objs = []

        # Tracks deletion-order dependency for databases without transactions
        # or ability to defer constraint checks. Only concrete model classes
        # should be included, as the dependencies exist only between actual
        # database tables; proxy models are represented here by their concrete
        # parent.
        self.dependencies = defaultdict(set)  # {model: {models}}

    def add(
        self,
        objs,
        source=None,
        nullable=False,
        reverse_dependency=False,
        ignore_new_records=False,
    ):
        """
        Add 'objs' to the collection of objects to be modified (deleted/updated).
        If the call is the result of a cascade, 'source' should be the model that
        caused it, and 'nullable' should be set to True if the relation can be null.

        Return a list of all objects that were not already collected.
        """
        if not objs:
            return []
        new_objs = []
        model = objs[0].__class__
        instances = self.data[model]
        for obj in objs:
            if obj not in instances:
                if ignore_new_records and obj._state.adding:
                    continue
                new_objs.append(obj)
        instances.update(new_objs)
        # Nullable relationships can be ignored -- they are nulled out before
        # deleting, and therefore do not affect the order in which objects have
        # to be deleted.
        if source is not None and not nullable:
            self.add_dependency(source, model, reverse_dependency=reverse_dependency)
        return new_objs

    def add_dependency(self, model, dependency, reverse_dependency=False):
        if reverse_dependency:
            model, dependency = dependency, model
        self.dependencies[model._meta.concrete_model].add(
            dependency._meta.concrete_model
        )
        self.data.setdefault(dependency, self.data.default_factory())

    def add_field_update(self, field, value, objs):
        """
        Schedule a field update. 'objs' must be a homogeneous iterable
        collection of model instances (e.g. a QuerySet).
        """
        self.field_updates[field, value].append(objs)

    def add_restricted_objects(self, field, objs):
        if objs:
            model = objs[0].__class__
            self.restricted_objects[model][field].update(objs)

    def clear_restricted_objects_from_set(self, model, objs):
        if model in self.restricted_objects:
            self.restricted_objects[model] = {
                field: items - objs
                for field, items in self.restricted_objects[model].items()
            }

    def clear_restricted_objects_from_queryset(self, model, qs):
        if model in self.restricted_objects:
            objs = set(
                qs.filter(
                    pk__in=[
                        obj.pk
                        for objs in self.restricted_objects[model].values()
                        for obj in objs
                    ]
                )
            )
            self.clear_restricted_objects_from_set(model, objs)

    @abstractmethod
    def _has_signal_listeners(self, model):
        pass

    def get_obj_batches(self, objs, fields):
        """
        Return the objs in suitably sized batches for the used connection.
        """
        field_names = [field.name for field in fields]
        conn_batch_size = max(
            connections[self.using].ops.bulk_batch_size(field_names, objs), 1
        )
        if len(objs) > conn_batch_size:
            return [
                objs[i : i + conn_batch_size]
                for i in range(0, len(objs), conn_batch_size)
            ]
        else:
            return [objs]

    @abstractmethod
    def collect(
        self,
        objs,
        source=None,
        nullable=False,
        collect_related=True,
        source_attr=None,
        reverse_dependency=False,
        keep_parents=False,
        fail_on_restricted=True,
    ):
        pass

    def related_objects(self, related_model, related_fields, objs):
        """
        Get a QuerySet of the related model to objs via related fields.
        """
        predicate = query_utils.Q.create(
            [(f"{related_field.name}__in", objs) for related_field in related_fields],
            connector=query_utils.Q.OR,
        )
        return related_model._base_manager.using(self.using).filter(predicate)

    def instances_with_model(self):
        for model, instances in self.data.items():
            for obj in instances:
                yield model, obj

    def sort(self):
        sorted_models = []
        concrete_models = set()
        models = list(self.data)
        while len(sorted_models) < len(models):
            found = False
            for model in models:
                if model in sorted_models:
                    continue
                dependencies = self.dependencies.get(model._meta.concrete_model)
                if not (dependencies and dependencies.difference(concrete_models)):
                    sorted_models.append(model)
                    concrete_models.add(model._meta.concrete_model)
                    found = True
            if not found:
                return
        self.data = {model: self.data[model] for model in sorted_models}
