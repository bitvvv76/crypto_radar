from datetime import datetime
from pathlib import Path
import shutil


PROJECT_DIR = Path(__file__).resolve().parent
DATABASE_FILE = PROJECT_DIR / "crypto_radar.db"
BACKUP_DIR = PROJECT_DIR / "backups"


def main():
    if not DATABASE_FILE.exists():
        print("Ошибка: база данных crypto_radar.db не найдена")
        return

    BACKUP_DIR.mkdir(exist_ok=True)

    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    backup_file = BACKUP_DIR / f"crypto_radar_{timestamp}.db"

    shutil.copy2(DATABASE_FILE, backup_file)

    print("Резервная копия базы создана")
    print("Исходная база:", DATABASE_FILE)
    print("Копия:", backup_file)


if __name__ == "__main__":
    main()