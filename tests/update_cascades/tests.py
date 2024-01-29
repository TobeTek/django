from .models import (
    Customer,
    CustomerAddress,
    DEFAULT_CITY_CENTER,
    A,
    B,
    Tree,
    UpdateCascadeRecursiveRef1 as RecursiveRef1,
    UpdateCascadeRecursiveRef2 as RecursiveRef2,
)
from django.test import TestCase
from django.db import DEFAULT_DB_ALIAS
from django.db.models.updates import UpdateCollector
from django.test import TestCase


class SimplifiedUpdateTests(TestCase):
    def setUp(self):
        ...

    def test_cascade_basic(self):
        a1 = A.objects.create(address="foo1")
        a2 = A.objects.create(address="foo2")
        B.objects.create(fk=a1)
        B.objects.create(fk=a1)
        B.objects.create(fk=a2)

        A.objects.filter(address="foo1").update(address="bar1")

        self.assertFalse(B.objects.filter(fk="foo1").exists())
        self.assertEqual(B.objects.filter(fk="foo2").count(), 1)
        self.assertEqual(B.objects.filter(fk="bar1").count(), 2)

    def test_cascade_tree(self):
        root = Tree.objects.create(name="root")
        root_c1 = Tree.objects.create(name="root_c1", parent=root)
        Tree.objects.create(name="root_c1_c1", parent=root_c1)
        root_c2 = Tree.objects.create(name="root_c2", parent=root)
        Tree.objects.create(name="root_c2_c1", parent=root_c2)
        
        Tree.objects.filter(name="root").update(name="new_root")
        
        self.assertTrue(Tree.objects.filter(name="new_root").exists())
        self.assertEqual(Tree.objects.filter(parent="new_root").count(), 2)

    def test_cascade_recursive(self):
        rv1 = RecursiveRef1.objects.create(name="foo")
        rv2 = RecursiveRef2.objects.create(fk=rv1)
        RecursiveRef1.objects.update(rev_fk=rv2)
        
        RecursiveRef1.objects.filter(name="foo").update(name="bar")
        
        self.assertQuerysetEqual(
            RecursiveRef1.objects.values_list("name", "rev_fk"),
            [("bar", "bar")],
            lambda x: x,
        )
        self.assertQuerysetEqual(
            RecursiveRef2.objects.values_list("fk", flat=True),
            ["bar"],
            lambda x: x,
        )

class OnUpdateTests(TestCase):
    def test_auto(self):
        ...
    
    def test_non_callable(self):
        ...
    
    def test_auto_nullable(self):
        ...
    
    def test_setvalue(self):
        ...
    
    def test_setnull(self):
        ...
    
    def test_setdefault(self):
        ...
    
    def test_setdefault_none(self):
        ...
    
    def test_cascade(self):
        ...
    
    def test_cascade_nullable(self):
        ...
    
    def test_protect(self):
        ...
    
    def test_protect_multiple(self):
        ...
    
    def test_protect_path(self):
        ...

    def test_do_nothing(self):
        ...
    
    def test_do_nothing_qscount(self):
        ...
    
    def test_inheritance_cascade_up(self):
        ...
    
    def test_inheritance_cascade_down(self):
        ...
    
    def test_cascade_from_child(self):
        ...
    
    def test_cascade_from_parent(self):
        ...
    
    def test_setnull_from_child(self):
        ...
    
    def test_setnull_from_parent(self):
        ...
    
    def test_o2o_setnull(self):
        ...
    
    def test_restrict(self):
        ...
    
    def test_restrict_multiple(self):
        ...
    
    def test_restrict_path_cascade_indirect(self):
        ...
    
    def test_restrict_path_cascade_direct(self):
        ...

    def test_restrict_path_cascade_indirect_diamond(self):
        ...
    
    def test_restrict_gfk_no_fast_delete(self):
        # gfk = Generic Foreign Key
        ...

class FastUpdateTests(TestCase):
    ...