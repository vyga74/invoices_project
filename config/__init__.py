import pymysql

# Django 6.x reikalauja "mysqlclient" >= 2.2.1,
# o PyMySQL install_as_MySQLdb pagal nutylėjimą apsimeta 1.4.6.
pymysql.version_info = (2, 2, 7, "final", 0)

pymysql.install_as_MySQLdb()