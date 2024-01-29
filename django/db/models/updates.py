from collections import Counter, defaultdict
from functools import partial, reduce
from itertools import chain
from operator import attrgetter, or_

from django.db import IntegrityError, connections, models, transaction
from django.db.models import query_utils, signals, sql
from django.db.models.utils import BaseCollector


def get_candidate_relations_to_update(opts):
    # The candidate relations are the ones that come from N-1 and 1-1 relations.
    # N-N  (i.e., many-to-many) relations aren't candidates for deletion.
    return (
        f
        for f in opts.get_fields(include_hidden=True)
        if f.auto_created and not f.concrete and (f.one_to_one or f.one_to_many)
    )


class UpdateCollector(BaseCollector):
    @property
    def fast_updates(self):
        return self.fast_mod_objs

    def _has_signal_listeners(self, model):
        return signals.pre_save.has_listeners(model) or signals.post_save.has_listeners(
            model
        )

    def can_fast_update(self, objs, from_field=None):
        """
        Determine if the objects in the given queryset-like or single object
        can be fast-updated. This can be done if there are no cascades, no
        parents and no signal listeners for the object class.

        The 'from_field' tells where we are coming from - we need this to
        determine if the objects are in fact to be deleted. Allow also
        skipping parent -> child -> parent chain preventing fast delete of
        the child.
        """
        from django.db.models.deletion import CASCADE, DO_NOTHING

        if from_field and from_field.remote_field.on_update is not CASCADE:
            return False
        if hasattr(objs, "_meta"):
            model = objs._meta.model
        elif hasattr(objs, "model") and hasattr(objs, "update"):
            model = objs.model
        else:
            return False
        if self._has_signal_listeners(model):
            return False
        # The use of from_field comes from the need to avoid cascade back to
        # parent when parent delete is cascading to child.
        opts = model._meta
        return (
            all(
                link == from_field
                for link in opts.concrete_model._meta.parents.values()
            )
            and
            # Foreign keys pointing to this model.
            all(
                related.field.remote_field.on_update is DO_NOTHING
                for related in get_candidate_relations_to_update(opts)
            )
            and (
                # Something like generic foreign key.
                not any(
                    hasattr(field, "bulk_related_objects")
                    for field in opts.private_fields
                )
            )
        )

    def get_update_batches(self, objs, fields):
        return super().get_obj_batches(objs, fields)

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
        """
        Add 'objs' to the collection of objects to be deleted as well as all
        parent instances.  'objs' must be a homogeneous iterable collection of
        model instances (e.g. a QuerySet).  If 'collect_related' is True,
        related objects will be handled by their respective on_delete handler.

        If the call is the result of a cascade, 'source' should be the model
        that caused it and 'nullable' should be set to True, if the relation
        can be null.

        If 'reverse_dependency' is True, 'source' will be deleted before the
        current model, rather than after. (Needed for cascading to parent
        models, the one case in which the cascade follows the forwards
        direction of an FK rather than the reverse direction.)

        If 'keep_parents' is True, data of parent model's will be not deleted.

        If 'fail_on_restricted' is False, error won't be raised even if it's
        prohibited to delete such objects due to RESTRICT, that defers
        restricted object checking in recursive calls where the top-level call
        may need to collect more objects to determine whether restricted ones
        can be deleted.
        """
        from django.db.models.deletion import (
            CASCADE,
            DO_NOTHING,
            PROTECT,
            ProtectedError,
            RestrictedError,
        )

        if self.can_fast_update(objs):
            self.fast_mod_objs.append(objs)
            return
        new_objs = self.add(
            objs,
            source,
            nullable,
            reverse_dependency=reverse_dependency,
            ignore_new_records=True,
        )
        if not new_objs:
            return

        model = new_objs[0].__class__

        if not keep_parents:
            # Recursively collect concrete model's parent models, but not their
            # related objects. These will be found by meta.get_fields()
            concrete_model = model._meta.concrete_model
            for ptr in concrete_model._meta.parents.values():
                if ptr:
                    parent_objs = [getattr(obj, ptr.name) for obj in new_objs]
                    self.collect(
                        parent_objs,
                        source=model,
                        source_attr=ptr.remote_field.related_name,
                        collect_related=False,
                        reverse_dependency=True,
                        fail_on_restricted=False,
                    )
        if not collect_related:
            return

        if keep_parents:
            parents = set(model._meta.get_parent_list())
        model_fast_updates = defaultdict(list)
        protected_objects = defaultdict(list)
        for related in get_candidate_relations_to_update(model._meta):
            # Preserve parent reverse relationships if keep_parents=True.
            if keep_parents and related.model in parents:
                continue
            field = related.field
            on_update = field.remote_field.on_update
            if on_update == DO_NOTHING:
                continue
            related_model = related.related_model
            if self.can_fast_update(related_model, from_field=field):
                model_fast_updates[related_model].append(field)
                continue
            batches = self.get_obj_batches(new_objs, [field])
            for batch in batches:
                sub_objs = self.related_objects(related_model, [field], batch)
                # Non-referenced fields can be deferred if no signal receivers
                # are connected for the related model as they'll never be
                # exposed to the user. Skip field deferring when some
                # relationships are select_related as interactions between both
                # features are hard to get right. This should only happen in
                # the rare cases where .related_objects is overridden anyway.
                if not (
                    sub_objs.query.select_related
                    or self._has_signal_listeners(related_model)
                ):
                    referenced_fields = set(
                        chain.from_iterable(
                            (rf.attname for rf in rel.field.foreign_related_fields)
                            for rel in get_candidate_relations_to_update(
                                related_model._meta
                            )
                        )
                    )
                    sub_objs = sub_objs.only(*tuple(referenced_fields))
                if getattr(on_update, "lazy_sub_objs", False) or sub_objs:
                    try:
                        on_update(self, field, sub_objs, self.using)
                    except ProtectedError as error:
                        key = "'%s.%s'" % (field.model.__name__, field.name)
                        protected_objects[key] += error.protected_objects
        if protected_objects:
            raise ProtectedError(
                "Cannot update some instances of model %r because they are "
                "referenced through protected foreign keys: %s."
                % (
                    model.__name__,
                    ", ".join(protected_objects),
                ),
                set(chain.from_iterable(protected_objects.values())),
            )
        for related_model, related_fields in model_fast_updates.items():
            batches = self.get_obj_batches(new_objs, related_fields)
            for batch in batches:
                sub_objs = self.related_objects(related_model, related_fields, batch)
                self.fast_mod_objs.append(sub_objs)
        for field in model._meta.private_fields:
            if hasattr(field, "bulk_related_objects"):
                # It's something like generic foreign key.
                sub_objs = field.bulk_related_objects(new_objs, self.using)
                self.collect(
                    sub_objs, source=model, nullable=True, fail_on_restricted=False
                )

        if fail_on_restricted:
            # Raise an error if collected restricted objects (RESTRICT) aren't
            # candidates for deletion also collected via CASCADE.
            for related_model, instances in self.data.items():
                self.clear_restricted_objects_from_set(related_model, instances)
            for qs in self.fast_mod_objs:
                self.clear_restricted_objects_from_queryset(qs.model, qs)
            if self.restricted_objects.values():
                restricted_objects = defaultdict(list)
                for related_model, fields in self.restricted_objects.items():
                    for field, objs in fields.items():
                        if objs:
                            key = "'%s.%s'" % (related_model.__name__, field.name)
                            restricted_objects[key] += objs
                if restricted_objects:
                    raise RestrictedError(
                        "Cannot delete some instances of model %r because "
                        "they are referenced through restricted foreign keys: "
                        "%s."
                        % (
                            model.__name__,
                            ", ".join(restricted_objects),
                        ),
                        set(chain.from_iterable(restricted_objects.values())),
                    )

    def update(self):
        # sort instance collections
        for model, instances in self.data.items():
            self.data[model] = sorted(instances, key=attrgetter("pk"))

        # if possible, bring the models in an order suitable for databases that
        # don't support transactions or cannot defer constraint checks until the
        # end of a transaction.
        self.sort()
        # number of objects updated for each model label
        update_counter = Counter()

        # Optimize for the case with a single obj and no dependencies
        if len(self.data) == 1 and len(instances) == 1:
            instance = list(instances)[0]
            if self.can_fast_update(instance):
                with transaction.mark_for_rollback_on_error(self.using):
                    count = sql.UpdateQuery(model).update_batch(
                        [instance.pk], using=self.using
                    )
                setattr(instance, model._meta.pk.attname, None)
                return count, {model._meta.label: count}

        with transaction.atomic(using=self.using, savepoint=False):
            # send pre_save signals
            for model, obj in self.instances_with_model():
                if not model._meta.auto_created:
                    signals.pre_save.send(
                        sender=model,
                        instance=obj,
                        using=self.using,
                        origin=self.origin,
                    )

            # fast updates
            for qs in self.fast_mod_objs:
                count = qs._update(using=self.using)
                if count:
                    update_counter[qs.model._meta.label] += count

            # update fields
            for (field, value), instances_list in self.field_updates.items():
                updates = []
                objs = []
                for instances in instances_list:
                    if (
                        isinstance(instances, models.QuerySet)
                        and instances._result_cache is None
                    ):
                        updates.append(instances)
                    else:
                        objs.extend(instances)
                if updates:
                    combined_updates = reduce(or_, updates)
                    combined_updates.update(**{field.name: value})
                if objs:
                    model = objs[0].__class__
                    query = sql.UpdateQuery(model)
                    query.update_batch(
                        list({obj.pk for obj in objs}), {field.name: value}, self.using
                    )

            # reverse instance collections
            for instances in self.data.values():
                instances.reverse()

            # update instances
            for model, instances in self.data.items():
                query = sql.UpdateQuery(model)
                pk_list = [obj.pk for obj in instances]
                count = query.update_batch(pk_list, self.using)
                if count:
                    update_counter[model._meta.label] += count

                if not model._meta.auto_created:
                    for obj in instances:
                        signals.post_save.send(
                            sender=model,
                            instance=obj,
                            using=self.using,
                            origin=self.origin,
                        )

        for model, instances in self.data.items():
            for instance in instances:
                setattr(instance, model._meta.pk.attname, None)
        return sum(update_counter.values()), dict(update_counter)
