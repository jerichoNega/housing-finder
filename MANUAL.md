# 🏠 Dutch Housing Finder

This manual explains how your Stealth Notifier system works, how to keep it running, and how to customize it.

---

## 🛠 1. How to Start/Restart the System
If you have closed your Mac and want to start the search again, follow these steps:

1.  Open your **Terminal** app.
2.  Paste these commands one by one:
    ```bash
    # Go to the project folder
    cd ~/housing-finder
    
    # Kill any old versions that might be stuck
    pkill -f "python3 main.py"
    
    # Start the script in the background
    python3 main.py &
    ```
3.  The script is now running. You can close the Terminal window, but **do not sleep the Mac**.

---

## 📡 2. How the System Works
The system is designed to be a "Stealth" tool so you don't get flagged by websites like Pararius.

- **Rotation:** Instead of checking everything at once, it checks **one city**, then waits **4-7 minutes**, then checks the next city.
- **Cities Covered:** Amsterdam, Utrecht, Eindhoven, Rotterdam, and Den Haag.
- **Price Limit:** Only shows places **under €1100**.
- **Memory:** It saves every house it sees into a file called `seen_listings.json`. It will **never** message you about the same house twice.

---

## 📱 3. Telegram Notifications
When a house is found, you get a message like this:
> 🏠 **New Listing Found!**
> 📍 **Flat Example Street**
> 💶 €1,050 pcm
> 📍 Eindhoven
> 🔗 [View Property]

**Action Plan:**
1.  Click the link immediately.
2.  Apply through the website or call the phone number if listed.
3.  **Mention your Sponsor:** Since you don't have a job yet, your first message should mention that your rent is fully guaranteed by a wealthy sponsor.

---

## ⚙️ 4. Customizing the Search
If you want to change the price or cities, you need to edit the code.

1.  Open the file: `~/housing-finder/main.py`
2.  Find the `TARGET_URLS` list at the top.
3.  You can add or remove URLs. To change the price, change the `0-1100` part of the link.
    - *Example for €1200:* `https://www.pararius.com/apartments/amsterdam/0-1200`

---

## 🚀 5. How to keep it running 24/7 (Advanced)
If you don't want to keep your MacBook open all night, you have two options:

1.  **"Amphetamine" App:** Download the free "Amphetamine" app from the Mac App Store. It allows you to keep your Mac awake even when the lid is closed.
2.  **A Server (VPS):** You can rent a tiny "Virtual Private Server" (like DigitalOcean or AWS) for about €5/month. You can put this script on that server, and it will run 24/7/365 without you ever needing to look at it.

---

## 📂 Project Structure
- `main.py`: The brain of the system.
- `.env`: Stores your Telegram Service Token and Chat ID.
- `seen_listings.json`: The memory file (prevents duplicate messages).
- `requirements.txt`: The list of software needed to run it.
