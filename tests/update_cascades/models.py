from django.db import models
from django.db import transaction


class A(models.Model):
    address = models.CharField(max_length=20, unique=True)


class B(models.Model):
    fk = models.ForeignKey(
        A,
        on_delete=models.CASCADE,
        on_update=models.CASCADE,
        to_field="address",
    )


class Tree(models.Model):
    parent = models.ForeignKey(
        "self",
        null=True,
        to_field="name",
        on_delete=models.CASCADE,
        on_update=models.CASCADE,
    )
    name = models.CharField(max_length=20, unique=True)


class UpdateCascadeRecursiveRef1(models.Model):
    name = models.CharField(max_length=20)
    rev_fk = models.ForeignKey(
        "UpdateCascadeRecursiveRef2",
        to_field="fk",
        on_delete=models.CASCADE,
        on_update=models.CASCADE,
        null=True,
        unique=True,
    )


class UpdateCascadeRecursiveRef2(models.Model):
    fk = models.ForeignKey(
        UpdateCascadeRecursiveRef1,
        to_field="name",
        on_delete=models.CASCADE,
        on_update=models.CASCADE,
    )


DEFAULT_CITY_CENTER = "DEFAULT_CITY_CENTER"


class CustomerAddress(models.Model):
    address = models.CharField(max_length=20)
    nearest_shop = models.ForeignKey(
        "self", on_delete=models.SET_NULL, on_update=models.SET_NULL, to_field="address"
    )
    city_center = models.ForeignKey(
        "self",
        on_delete=models.SET_DEFAULT,
        on_update=models.SET_DEFAULT,
        to_field="address",
        default=DEFAULT_CITY_CENTER,
    )


class Customer(models.Model):
    name = models.CharField(max_length=20, unique=True)
    address = models.ForeignKey(
        CustomerAddress,
        on_delete=models.CASCADE,
        on_update=models.CASCADE,
        to_field="address",
    )
    protected_address = models.ForeignKey(
        CustomerAddress,
        on_delete=models.PROTECT,
        on_update=models.PROTECT,
        to_field="address",
    )
    do_nothing_address = models.ForeignKey(
        CustomerAddress,
        on_delete=models.DO_NOTHING,
        on_update=models.DO_NOTHING,
        to_field="address",
    )
