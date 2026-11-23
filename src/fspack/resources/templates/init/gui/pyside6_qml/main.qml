// $project_name QML 主窗口
import QtQuick 2.15
import QtQuick.Controls 2.15
import QtQuick.Layouts 1.15

ApplicationWindow {
    title: "$project_name"
    width: 400
    height: 200
    visible: true

    ColumnLayout {
        anchors.centerIn: parent

        Label {
            id: counter
            text: "点击按钮 0 次"
            Layout.alignment: Qt.AlignHCenter
            font.pixelSize: 16
        }

        Button {
            text: "点我"
            Layout.alignment: Qt.AlignHCenter
            onClicked: {
                var n = parseInt(counter.text.match(/\d+/)[0]) + 1
                counter.text = "点击按钮 " + n + " 次"
            }
        }
    }
}
