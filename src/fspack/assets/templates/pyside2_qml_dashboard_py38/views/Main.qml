import QtQuick 2.15
import QtQuick.Controls 2.15
import QtQuick.Layouts 1.15

ApplicationWindow {
    id: root
    visible: true
    width: 960
    height: 620
    title: "WSL Dashboard Style · PySide2"

    // ========== 背景色随主题切换 ==========
    background: Rectangle {
        color: Theme.isDark ? "#1A1B26" : "#F5F6F8"
        Behavior on color { ColorAnimation { duration: 200 } }
    }

    // ========== 主布局：侧边栏 + 内容 ==========
    RowLayout {
        anchors.fill: parent
        spacing: 0

        // ---------- 左侧侧边栏 ----------
        Sidebar {
            Layout.preferredWidth: 200
            Layout.fillHeight: true
        }

        // ---------- 右侧主内容 ----------
        ContentArea {
            Layout.fillWidth: true
            Layout.fillHeight: true
        }
    }
}