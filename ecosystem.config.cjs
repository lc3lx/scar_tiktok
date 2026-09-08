module.exports = {
  apps: [
    {
      name: "scar-instagram",
      script: "app.py",
      interpreter: "/home/web/tik/.venv/bin/python",
      cwd: "/home/web/tik",
      instances: 1,
      autorestart: true,
      watch: false,
      max_memory_restart: "800M",
      env: {
        HOST: "0.0.0.0",
        PORT: "5050",
        PYTHONUNBUFFERED: "1",
        DISPLAY: ":99",
        IG_PROXY: "178.93.74.74:46459:ilIXTcXCyPrJyYm:7LMX2TY1odthIoK",
      },
    },
  ],
};
