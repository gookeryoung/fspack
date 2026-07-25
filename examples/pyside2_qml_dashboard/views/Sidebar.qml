import QtQuick 2.15
import QtQuick.Controls 2.15
import QtQuick.Layouts 1.15

Pane {
    id: sidebar
    padding: 0

    // 侧栏背景：深色模式深蓝黑，浅色模式纯白
    background: Rectangle {
        color: Theme.isDark ? "#16161E" : "#FFFFFF"
        Behavior on color { ColorAnimation { duration: 200 } }
    }

    // 右侧 1px 分割线
    Rectangle {
        anchors.right: parent.right
        width: 1
        height: parent.height
        color: Theme.isDark ? "#2A2B3A" : "#E8E8E8"
    }

    // ========== 当前选中页（供 ContentArea 读取） ==========
    property string currentPage: "home"

    ColumnLayout {
        anchors.fill: parent
        anchors.margins: 0
        spacing: 0

        // ---------- Logo 区 ----------
        Item {
            Layout.fillWidth: true
            Layout.preferredHeight: 64
            Layout.leftMargin: 20
            Layout.rightMargin: 16

            RowLayout {
                anchors.verticalCenter: parent.verticalCenter
                spacing: 10
                // 用纯色圆角矩形代替图标
                Rectangle {
                    width: 28; height: 28; radius: 6
                    color: Theme.isDark ? "#7AA2F7" : "#4A90D9"
                    Label {
                        anchors.centerIn: parent
                        text: "W"
                        color: "#FFFFFF"
                        font.pixelSize: 14
                        font.bold: true
                    }
                }
                Label {
                    text: "Dashboard"
                    font.pixelSize: 15
                    font.bold: true
                    color: Theme.isDark ? "#C0C0CF" : "#333333"
                }
            }
        }

        // ---------- 导航项列表 ----------
        NavItem {
            iconText: "🏠"; label: "Home"; pageId: "home"
            selected: sidebar.currentPage === "home"
            onClicked: { sidebar.currentPage = "home" }
        }
        NavItem {
            iconText: "＋"; label: "Add Instance"; pageId: "add"
            selected: sidebar.currentPage === "add"
            onClicked: { sidebar.currentPage = "add" }
        }
        NavItem {
            iconText: "🔌"; label: "USB Devices"; pageId: "usb"
            selected: sidebar.currentPage === "usb"
            onClicked: { sidebar.currentPage = "usb" }
        }
        NavItem {
            iconText: "🌐"; label: "Network"; pageId: "network"
            selected: sidebar.currentPage === "network"
            onClicked: { sidebar.currentPage = "network" }
        }

        Item { Layout.fillHeight: true }  // 弹性撑开

        // ---------- 底部：设置 + 暗色切换 ----------
        NavItem {
            iconText: "⚙"; label: "Settings"; pageId: "settings"
            selected: sidebar.currentPage === "settings"
            onClicked: { sidebar.currentPage = "settings" }
        }

        // 暗色模式开关（Fluent 风 Toggle）
        Rectangle {
            Layout.fillWidth: true
            Layout.leftMargin: 14
            Layout.rightMargin: 14
            Layout.bottomMargin: 16
            Layout.preferredHeight: 36
            radius: 8
            color: Theme.isDark ? "#2A2B3A" : "#F0F1F4"
            border.color: Theme.isDark ? "#3A3B4A" : "#E0E0E0"
            border.width: 1

            RowLayout {
                anchors.fill: parent
                anchors.margins: 8
                spacing: 8
                Label {
                    text: "🌙"
                    font.pixelSize: 14
                }
                Label {
                    text: "Dark Mode"
                    font.pixelSize: 12
                    color: Theme.isDark ? "#A0A0B0" : "#555"
                    Layout.fillWidth: true
                }
                // 自定义开关
                Rectangle {
                    width: 36; height: 20; radius: 10
                    color: Theme.isDark ? "#7AA2F7" : "#CCC"
                    Behavior on color { ColorAnimation { duration: 150 } }
                    MouseArea {
                        anchors.fill: parent
                        onClicked: Theme.setDark(!Theme.isDark)
                    }
                    Rectangle {
                        width: 16; height: 16; radius: 8
                        color: "#FFFFFF"
                        x: Theme.isDark ? 18 : 2
                        anchors.verticalCenter: parent.verticalCenter
                        Behavior on x { NumberAnimation { duration: 150 } }
                    }
                }
            }
        }
    }
}