from app.auth.service import create_owner_user,create_member_user

if __name__ == "__main__":
    email = input("Owner email: ").strip()
    password = input("Owner password: ").strip()
    print(create_member_user(email, password))
