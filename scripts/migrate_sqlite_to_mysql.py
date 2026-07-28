"""
SQLite 到 MySQL 数据迁移脚本。

用于将现有 SQLite 数据库迁移到 MySQL。
"""

import sqlite3
import pymysql
import pymysql.cursors
from pathlib import Path
from typing import Any, Dict, List, Optional


# MySQL 连接配置
MYSQL_CONFIG = {
    'host': '127.0.0.1',
    'port': 3306,
    'user': 'awmcbot',
    'password': 'kYPXZAkWztjZyp2L',
    'database': 'awmcbot',
    'charset': 'utf8mb4',
}

# 表名前缀
TABLE_PREFIX = 'maimaidx_'

# SQLite 数据库路径
DATA_DIR = Path('/www/bot/.venv/lib/python3.12/site-packages/nonebot_plugin_maimaidx/data')


def get_mysql_connection():
    """获取 MySQL 连接。"""
    return pymysql.connect(
        **MYSQL_CONFIG,
        cursorclass=pymysql.cursors.DictCursor,
        autocommit=False,
    )


def sqlite_to_mysql_type(sqlite_type: str) -> str:
    """将 SQLite 类型转换为 MySQL 类型。"""
    sqlite_type = sqlite_type.upper()
    if 'INTEGER' in sqlite_type:
        if 'PRIMARY KEY' in sqlite_type:
            return 'BIGINT AUTO_INCREMENT PRIMARY KEY'
        return 'BIGINT'
    elif 'TEXT' in sqlite_type:
        if 'PRIMARY KEY' in sqlite_type:
            return 'VARCHAR(255) PRIMARY KEY'
        return 'TEXT'
    elif 'REAL' in sqlite_type or 'FLOAT' in sqlite_type or 'DOUBLE' in sqlite_type:
        return 'DOUBLE'
    elif 'BLOB' in sqlite_type:
        return 'LONGBLOB'
    elif 'BOOLEAN' in sqlite_type:
        return 'TINYINT(1)'
    return 'TEXT'


def create_mysql_table(mysql_conn, table_name: str, columns: List[Dict]):
    """创建 MySQL 表。"""
    prefixed_name = f"{TABLE_PREFIX}{table_name}"
    
    # 检查表是否已存在
    with mysql_conn.cursor() as cur:
        cur.execute(f"SHOW TABLES LIKE '{prefixed_name}'")
        if cur.fetchone():
            print(f"表 {prefixed_name} 已存在，跳过创建")
            return
    
    # 创建表
    column_defs = []
    primary_keys = []
    indexes = []
    
    for col in columns:
        col_name = col['name']
        col_type = sqlite_to_mysql_type(col['type'])
        
        if 'PRIMARY KEY' in col_type and 'AUTO_INCREMENT' not in col_type:
            # 复合主键的情况
            primary_keys.append(col_name)
            col_type = col_type.replace(' PRIMARY KEY', '')
        
        column_defs.append(f"    `{col_name}` {col_type},")
    
    # 移除最后一个逗号
    if column_defs:
        column_defs[-1] = column_defs[-1].rstrip(',')
    
    if primary_keys:
        column_defs.append(f"    PRIMARY KEY ({', '.join(primary_keys)})")
    
    create_sql = f"""
CREATE TABLE `{prefixed_name}` (
{chr(10).join(column_defs)}
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
"""
    
    print(f"创建表 {prefixed_name}...")
    print(create_sql)
    
    with mysql_conn.cursor() as cur:
        cur.execute(create_sql)
    mysql_conn.commit()


def migrate_table(sqlite_path: Path, table_name: str, mysql_conn):
    """迁移单个表。"""
    if not sqlite_path.exists():
        print(f"SQLite 文件不存在: {sqlite_path}")
        return
    
    sqlite_conn = sqlite3.connect(str(sqlite_path))
    sqlite_conn.row_factory = sqlite3.Row
    
    # 获取表结构
    cursor = sqlite_conn.cursor()
    cursor.execute(f"PRAGMA table_info({table_name})")
    columns = [{'name': row[1], 'type': row[2]} for row in cursor.fetchall()]
    
    if not columns:
        print(f"表 {table_name} 不存在或为空")
        sqlite_conn.close()
        return
    
    # 创建 MySQL 表
    create_mysql_table(mysql_conn, table_name, columns)
    
    # 迁移数据
    cursor.execute(f"SELECT * FROM {table_name}")
    rows = cursor.fetchall()
    
    if not rows:
        print(f"表 {table_name} 没有数据")
        sqlite_conn.close()
        return
    
    print(f"迁移 {len(rows)} 条数据到 {TABLE_PREFIX}{table_name}...")
    
    # 构建插入语句
    col_names = [col['name'] for col in columns]
    placeholders = ', '.join(['%s'] * len(col_names))
    col_names_str = ', '.join([f'`{name}`' for name in col_names])
    insert_sql = f"INSERT IGNORE INTO `{TABLE_PREFIX}{table_name}` ({col_names_str}) VALUES ({placeholders})"
    
    # 批量插入
    batch_size = 1000
    for i in range(0, len(rows), batch_size):
        batch = rows[i:i+batch_size]
        data = [tuple(row[col] for col in col_names) for row in batch]
        with mysql_conn.cursor() as cur:
            cur.executemany(insert_sql, data)
        mysql_conn.commit()
        print(f"  已迁移 {min(i+batch_size, len(rows))}/{len(rows)} 条")
    
    sqlite_conn.close()


def migrate_break_db(mysql_conn):
    """迁移 break.db。"""
    sqlite_path = DATA_DIR / 'break' / 'break.db'
    tables = [
        'break_users',
        'break_daily_usage',
        'break_group_checkin',
        'break_config',
        'break_log',
        'break_guess_daily',
        'break_service_daily',
        'break_daily_reward',
        'break_red_packet',
        'break_red_packet_claim',
        'break_makeup_checkin',
        'break_gamble_pool',
    ]
    
    for table in tables:
        migrate_table(sqlite_path, table, mysql_conn)


def migrate_account_db(mysql_conn):
    """迁移 account.db。"""
    sqlite_path = DATA_DIR / 'account' / 'account.db'
    tables = [
        'account_bindings',
        'account_operation_log',
    ]
    
    for table in tables:
        migrate_table(sqlite_path, table, mysql_conn)


def migrate_playcount_db(mysql_conn):
    """迁移 playcount.db。"""
    sqlite_path = DATA_DIR / 'playcount' / 'playcount.db'
    tables = [
        'user_credentials',
        'play_count_records',
        'user_prober_tokens',
    ]
    
    for table in tables:
        migrate_table(sqlite_path, table, mysql_conn)


def migrate_qq_bind_db(mysql_conn):
    """迁移 qq_bind.db。"""
    sqlite_path = DATA_DIR / 'qq_bind' / 'qq_bind.db'
    tables = [
        'qq_bind',
    ]
    
    for table in tables:
        migrate_table(sqlite_path, table, mysql_conn)


def migrate_lxns_db(mysql_conn):
    """迁移 lxns.db。"""
    sqlite_path = DATA_DIR / 'lxns' / 'lxns.db'
    tables = [
        'lxns_users',
    ]
    
    for table in tables:
        migrate_table(sqlite_path, table, mysql_conn)


def main():
    """主函数：执行所有迁移。"""
    print("开始迁移 SQLite 数据到 MySQL...")
    print(f"MySQL 配置: {MYSQL_CONFIG['host']}:{MYSQL_CONFIG['port']}/{MYSQL_CONFIG['database']}")
    print(f"表名前缀: {TABLE_PREFIX}")
    print()
    
    mysql_conn = get_mysql_connection()
    
    try:
        print("=== 迁移 break.db ===")
        migrate_break_db(mysql_conn)
        print()
        
        print("=== 迁移 account.db ===")
        migrate_account_db(mysql_conn)
        print()
        
        print("=== 迁移 playcount.db ===")
        migrate_playcount_db(mysql_conn)
        print()
        
        print("=== 迁移 qq_bind.db ===")
        migrate_qq_bind_db(mysql_conn)
        print()
        
        print("=== 迁移 lxns.db ===")
        migrate_lxns_db(mysql_conn)
        print()
        
        print("迁移完成！")
        
    finally:
        mysql_conn.close()


if __name__ == '__main__':
    main()
